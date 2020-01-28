import io
import json
import os
import tarfile
import random
import ssdeep
import hashlib
import traceback
import re
import email.header
import sys
import requests

from retrying import retry, RetryError

from assemblyline_v4_service.common.task import MaxExtractedExceeded
from assemblyline_v4_service.common.result import Result, ResultSection, BODY_FORMAT, Heuristic
from assemblyline_v4_service.common.base import ServiceBase

from assemblyline.common.str_utils import safe_str
from assemblyline.common.identify import tag_to_extension
from assemblyline.common.exceptions import RecoverableError, ChainException

from cuckoo.whitelist import wlist_check_hash, wlist_check_dropped

# CUCKOO_API_PORT = "8090"
CUCKOO_API_SUBMIT = "tasks/create/file"
CUCKOO_API_QUERY_TASK = "tasks/view/%s"
CUCKOO_API_DELETE_TASK = "tasks/delete/%s"
CUCKOO_API_QUERY_REPORT = "tasks/report/%s"
CUCKOO_API_QUERY_PCAP = "pcap/get/%s"
CUCKOO_API_QUERY_MACHINES = "machines/list"
CUCKOO_API_QUERY_MACHINE_INFO = "machines/view/%s"
CUCKOO_API_QUERY_HOST_STATUS = "cuckoo/status"
CUCKOO_POLL_DELAY = 5
GUEST_VM_START_TIMEOUT = 75

SUPPORTED_EXTENSIONS = [
    "cpl",
    "dll",
    "exe",
    "pdf",
    "doc",
    "docx",
    "rtf",
    "mht",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "pps",
    "ppsx",
    "pptm",
    "potm",
    "potx",
    "ppsm",
    "htm",
    "html",
    "jar",
    "rar",
    "swf",
    "py",
    "pyc",
    "vbs",
    "msi",
    "ps1",
    "msg",
    "eml",
    "js",
    "wsf",
    "elf",
    "bin",
    "hta",
    "zip",
    "lnk",
    "hwp",
    "pub",
    "zip",
]


class CuckooTimeoutException(Exception):
    """Exception class for timeouts"""
    pass


class MissingCuckooReportException(Exception):
    """Exception class for missing reports"""
    pass


class CuckooProcessingException(Exception):
    """Exception class for processing errors"""
    pass


class CuckooVMBusyException(Exception):
    """Exception class for busy VMs"""
    pass


class MaxFileSizeExceeded(Exception):
    """Exception class for files that are too large"""
    pass


def _exclude_chain_ex(ex):
    """Use this with some of the @retry decorators to only retry if the exception
    ISN'T a RecoverableException or NonRecoverableException"""
    return not isinstance(ex, ChainException)


def _retry_on_none(result):
    return result is None


"""
    The following parameters are available for customization before sending a task to the cuckoo server:

    * ``file`` *(required)* - sample file (multipart encoded file content)
    * ``package`` *(optional)* - analysis package to be used for the analysis
    * ``timeout`` *(optional)* *(int)* - analysis timeout (in seconds)
    * ``options`` *(optional)* - options to pass to the analysis package
    * ``custom`` *(optional)* - custom string to pass over the analysis and the processing/reporting modules
    * ``memory`` *(optional)* - enable the creation of a full memory dump of the analysis machine
    * ``enforce_timeout`` *(optional)* - enable to enforce the execution for the full timeout value
"""


class CuckooTask(dict):
    def __init__(self, sample, **kwargs):
        super(CuckooTask, self).__init__()
        self.file = sample
        self.update(kwargs)
        self.id = None
        self.submitted = False
        self.completed = False
        self.report = None
        self.errors = []
        self.machine_info = None


# noinspection PyBroadException
# noinspection PyGlobalUndefined
class Cuckoo(ServiceBase):
    SERVICE_CLASSIFICATION = ""  # will default to unrestricted

    SERVICE_DEFAULT_CONFIG = {
        "dedup_similar_percent": 80,

        # If given a DLL without being told what function(s) to execute,
        # try to execute at most this many of the exports
        "max_dll_exports_exec": 5
    }

    def __init__(self, config=None):
        super(Cuckoo, self).__init__(config)
        self.cfg = config
        self.vm_xml = None
        self.vm_snapshot_xml = None
        self.file_name = None
        self.base_url = None
        self.submit_url = None
        self.query_task_url = None
        self.delete_task_url = None
        self.query_report_url = None
        self.query_pcap_url = None
        self.query_machines_url = None
        self.query_machine_info_url = None
        self.query_host_url = None
        self.task = None
        self.file_res = None
        self.cuckoo_task = None
        self.al_report = None
        self.session = None
        self.enabled_routes = None
        self.ssdeep_match_pct = 0
        self.restart_interval = 0
        self.machines = None
        self.auth_header = None

    # noinspection PyUnresolvedReferences
    def import_service_deps(self):
        global generate_al_result, pefile
        from cuckoo.cuckooresult import generate_al_result
        import pefile

    def set_urls(self):
        base_url = "http://%s:%s" % (self.cfg['remote_host_ip'], self.cfg['remote_host_port'])
        self.submit_url = "%s/%s" % (base_url, CUCKOO_API_SUBMIT)
        self.query_task_url = "%s/%s" % (base_url, CUCKOO_API_QUERY_TASK)
        self.delete_task_url = "%s/%s" % (base_url, CUCKOO_API_DELETE_TASK)
        self.query_report_url = "%s/%s" % (base_url, CUCKOO_API_QUERY_REPORT)
        self.query_pcap_url = "%s/%s" % (base_url, CUCKOO_API_QUERY_PCAP)
        self.query_machines_url = "%s/%s" % (base_url, CUCKOO_API_QUERY_MACHINES)
        self.query_machine_info_url = "%s/%s" % (base_url, CUCKOO_API_QUERY_MACHINE_INFO)
        self.query_host_url = "%s/%s" % (base_url, CUCKOO_API_QUERY_HOST_STATUS)

    def start(self):
        self.auth_header = {'Authorization': self.cfg['auth_header_value']}
        self.import_service_deps()
        self.ssdeep_match_pct = int(self.cfg.get("dedup_similar_percent", 80))
        self.log.debug("Cuckoo started!")

    # noinspection PyTypeChecker
    def execute(self, request):
        self.session = requests.Session()
        self.set_urls()
        self.task = request.task
        request.result = Result()

        # Setting working directory for request
        request._working_directory = self.working_directory

        self.file_res = request.result
        file_content = request.file_contents
        self.cuckoo_task = None
        self.al_report = None
        self.file_name = os.path.basename(request.file_name)

        # Check the filename to see if it's mime encoded
        mime_re = re.compile(r"^=\?.*\?=$")
        if mime_re.match(self.file_name):
            self.log.debug("Found a mime encoded filename, will try and decode")
            try:
                decoded_filename = email.header.decode_header(self.file_name)
                new_filename = decoded_filename[0][0].decode(decoded_filename[0][1])
                self.log.info("Using decoded filename %s" % new_filename)
                self.file_name = new_filename
            except:
                new_filename = generate_random_words(1)
                self.log.error(
                    "Problem decoding filename. Using randomly generated filename %s. Error: %s " %
                    (new_filename, traceback.format_exc())
                )
                self.file_name = new_filename

        # Check the file extension
        original_ext = self.file_name.rsplit('.', 1)
        tag_extension = tag_to_extension.get(self.task.file_type)

        # Poorly name var to track keyword arguments to pass into cuckoo's 'submit' function
        kwargs = dict()
        # the 'options' kwargs
        task_options = []

        # NOTE: Cuckoo still tries to identify files itself, so we only force the extension/package
        # if the user specifies one. However, we go through the trouble of renaming the file because
        # the only way to have certain modules run is to use the appropriate suffix (.jar, .vbs, etc.)

        # Check for a valid tag
        if tag_extension is not None and 'unknown' not in self.task.file_type:
            file_ext = tag_extension
        # Check if the file was submitted with an extension
        elif len(original_ext) == 2:
            submitted_ext = original_ext[1]
            if submitted_ext not in SUPPORTED_EXTENSIONS:
                # This is the case where the submitted file was NOT identified, and  the provided extension
                # isn't in the list of extensions that we explicitly support.
                self.log.debug("Cuckoo is exiting because it doesn't support the provided file type.")
                return
            else:
                if submitted_ext == "bin":
                    kwargs["package"] = "bin"
                # This is a usable extension. It might not run (if the submitter has lied to us).
                file_ext = '.' + submitted_ext
        else:
            # This is unknown without an extension that we accept/recognize.. no scan!
            self.log.info(
                "Cuckoo is exiting because the file type could not be identified. %s %s" %
                (tag_extension, self.task.file_type)
            )
            return

        # Rename based on the found extension.
        if file_ext and self.task.sha256:
            self.file_name = original_ext[0] + file_ext

        # Parse user args
        generate_report = None
        dump_processes = None
        dll_function = None
        arguments = None
        dump_memory = None
        no_monitor = None
        custom_options = None

        for param in self.cfg:
            if param == "analysis_timeout":
                kwargs['timeout'] = self.cfg.get(param, None)
            elif param == "generate_report":
                generate_report = self.cfg.get(param, None)
            elif param == "dump_processes":
                dump_processes = self.cfg.get(param, None)
            elif param == "dll_function":
                dll_function = self.cfg.get(param, None)
            elif param == "arguments":
                arguments = self.cfg.get(param, None)
            elif param == "dump_memory":
                dump_memory = self.cfg.get(param, None)
            elif param == "no_monitor":
                no_monitor = self.cfg.get(param, None)
            elif param == "enforce_timeout":
                kwargs['enforce_timeout'] = self.cfg.get(param, None)
            elif param == "custom_options":
                custom_options = self.cfg.get(param, None)

        if generate_report is True:
            self.log.debug("Setting generate_report flag.")

        if dump_processes is True:
            self.log.debug("Setting procmemdump flag in task options")
            task_options.append('procmemdump=yes')

        # Do DLL specific stuff
        if dll_function:
            task_options.append('function={}'.format(dll_function))

            # Check to see if there's commas in the dll_function
            if "|" in dll_function:
                kwargs["package"] = "dll_multi"

        exports_available = []

        if arguments:
            task_options.append('arguments={}'.format(arguments))

        if dump_memory and request.task.depth == 0:
            # Full system dump and volatility scan
            kwargs['memory'] = True

        if no_monitor:
            task_options.append("free=yes")

        kwargs['options'] = ','.join(task_options)
        if custom_options is not None:
            kwargs['options'] += ",%s" % custom_options

        self.cuckoo_task = CuckooTask(self.file_name,
                                      **kwargs)

        try:
            self.machines = self.cuckoo_query_machines()
            self.cuckoo_submit(file_content)
            if self.cuckoo_task.report:

                try:
                    machine_name = None
                    report_info = self.cuckoo_task.report.get('info', {})
                    machine = report_info.get('machine', {})

                    if isinstance(machine, dict):
                        machine_name = machine.get('name')

                    if machine_name is None:
                        self.log.debug('Unable to retrieve machine name from result.')
                        guest_ip = ""
                    else:
                        guest_ip = self.report_machine_info(machine_name)
                    self.log.debug("Generating AL Result from Cuckoo results..")
                    success = generate_al_result(self.cuckoo_task.report,
                                                 self.file_res,
                                                 request,
                                                 file_ext,
                                                 guest_ip,
                                                 self.SERVICE_CLASSIFICATION)
                    if success is False:
                        err_str = self.get_errors()
                        if self.cuckoo_task and self.cuckoo_task.id is not None:
                            self.cuckoo_delete_task(self.cuckoo_task.id)
                        raise CuckooProcessingException("Cuckoo was unable to process this file. %s",
                                                        err_str)
                except RecoverableError as e:
                    self.log.info("Recoverable error. Error message: %s" % e.message)
                    if self.cuckoo_task and self.cuckoo_task.id is not None:
                        self.cuckoo_delete_task(self.cuckoo_task.id)
                    raise
                except Exception as e:
                    self.log.exception("Error generating AL report: ")
                    if self.cuckoo_task and self.cuckoo_task.id is not None:
                        self.cuckoo_delete_task(self.cuckoo_task.id)
                    raise CuckooProcessingException(
                        "Unable to generate cuckoo al report for task %s: %s" %
                        (safe_str(self.cuckoo_task.id), safe_str(e))
                    )

                # Get the max size for extract files, used a few times after this
                request.max_file_size = self.cfg['max_file_size']
                max_extracted_size = request.max_file_size

                if generate_report is True:
                    self.log.debug("Generating cuckoo report tar.gz.")

                    # Submit cuckoo analysis report archive as a supplementary file
                    tar_report = self.cuckoo_query_report(self.cuckoo_task.id, fmt='all', params={'tar': 'gz'})
                    if tar_report is not None:
                        tar_file_name = "cuckoo_report.tar.gz"
                        tar_report_path = os.path.join(self.working_directory, tar_file_name)
                        try:
                            report_file = open(tar_report_path, 'wb')
                            report_file.write(tar_report)
                            report_file.close()
                            self.task.add_supplementary(tar_report_path, tar_file_name,
                                                        "Cuckoo Sandbox analysis report archive (tar.gz)")
                        except:
                            self.log.exception(
                                "Unable to add tar of complete report for task %s" % self.cuckoo_task.id)

                        # Attach report.json as a supplementary file. This is duplicating functionality
                        # a little bit, since this information is included in the JSON result section
                        try:
                            tar_obj = tarfile.open(tar_report_path)
                            if "reports/report.json" in tar_obj.getnames():
                                report_json_path = os.path.join(self.working_directory, "reports", "report.json")
                                tar_obj.extract("reports/report.json", path=self.working_directory)
                                self.task.add_supplementary(
                                    report_json_path,
                                    "report.json",
                                    "Cuckoo Sandbox report (json)"
                                )
                            tar_obj.close()
                        except:
                            self.log.exception(
                                "Unable to add report.json for task %s. Exception: %s" %
                                (self.cuckoo_task.id, traceback.format_exc())
                            )

                        # Check for any extra files in full report to add as extracted files
                        # special 'supplementary' directory
                        # memory artifacts
                        try:
                            # 'supplementary' files
                            tar_obj = tarfile.open(tar_report_path)
                            supplementary_files = [x.name for x in tar_obj.getmembers()
                                                   if x.name.startswith("supplementary") and x.isfile()]
                            for f in supplementary_files:
                                sup_file_path = os.path.join(self.working_directory, f)
                                tar_obj.extract(f, path=self.working_directory)
                                self.task.add_supplementary(sup_file_path, "Supplementary File",
                                                            display_name=f)

                            # process memory dump related
                            memdesc_lookup = {
                                "py": "IDA script to load process memory",
                                "dmp": "Process Memory Dump",
                                "exe_": "EXE Extracted from Memory Dump"
                            }
                            for f in [x.name for x in tar_obj.getmembers() if
                                      x.name.startswith("memory") and x.isfile()]:
                                mem_file_path = os.path.join(self.working_directory, f)
                                tar_obj.extract(f, path=self.working_directory)
                                # Lookup a more descriptive name, depending the filename suffix
                                filename_suffix = f.split(".")[-1]
                                memdesc = memdesc_lookup.get(filename_suffix, "Process Memory Artifact")
                                if filename_suffix == "py":
                                    self.task.add_supplementary(mem_file_path, memdesc, display_name=f)
                                else:
                                    mem_filesize = os.stat(mem_file_path).st_size
                                    try:
                                        self.task.add_extracted(mem_file_path, f, memdesc)
                                    except MaxFileSizeExceeded:
                                        self.file_res.add_section(ResultSection(
                                            title_text="Extracted file too large to add",
                                            body="Extracted file %s is %d bytes, which is larger than the maximum size "
                                                 "allowed for extracted files (%d). You can still access this file "
                                                 "by downloading the 'cuckoo_report.tar.gz' supplementary file" %
                                                 (f, mem_filesize, max_extracted_size)
                                        ))

                            # Extract buffers and anything extracted
                            extracted_buffers = [x.name for x in tar_obj.getmembers()
                                                 if x.name.startswith("buffer") and x.isfile()]
                            for f in extracted_buffers:
                                buffer_file_path = os.path.join(self.working_directory, f)
                                tar_obj.extract(f, path=self.working_directory)
                                self.task.add_extracted(buffer_file_path, f, "Extracted buffer")
                            for f in [x.name for x in tar_obj.getmembers() if
                                      x.name.startswith("extracted") and x.isfile()]:
                                extracted_file_path = os.path.join(self.working_directory, f)
                                tar_obj.extract(f, path=self.working_directory)
                                self.task.add_extracted(extracted_file_path, f, "Cuckoo extracted file")
                            tar_obj.close()
                        except:
                            self.log.exception(
                                "Unable to extra file(s) for task %s. Exception: %s" %
                                (self.cuckoo_task.id, traceback.format_exc())
                            )

                if len(exports_available) > 0 and kwargs.get("package", "") == "dll_multi":
                    max_dll_exports = self.cfg.get(
                        "max_dll_exports_exec",
                        self.SERVICE_DEFAULT_CONFIG["max_dll_exports_exec"]
                    )
                    dll_multi_section = ResultSection(
                        title_text="Executed multiple DLL exports",
                        body="Executed the following exports from the DLL: %s" % ",".join(exports_available[:max_dll_exports])
                    )
                    if len(exports_available) > max_dll_exports:
                        dll_multi_section.add_line("There were %d other exports: %s" %
                                                   ((len(exports_available) - max_dll_exports),
                                                    ",".join(exports_available[max_dll_exports:])))

                    self.file_res.add_section(dll_multi_section)

                self.log.debug("Checking for dropped files and pcap.")
                # Submit dropped files and pcap if available:
                self.check_dropped(request, self.cuckoo_task.id)
                self.check_pcap(self.cuckoo_task.id)

                if BODY_FORMAT.contains_value("JSON") and request.task.deep_scan:
                    # Attach report as json as the last result section
                    report_json_section = ResultSection(
                        'Full Cuckoo report',
                        self.SERVICE_CLASSIFICATION,
                        body_format=BODY_FORMAT.JSON,
                        body=self.cuckoo_task.report
                    )
                    self.file_res.add_section(report_json_section)

            else:
                # We didn't get a report back.. cuckoo has failed us
                self.log.info("Raising recoverable error for running job.")
                if self.cuckoo_task and self.cuckoo_task.id is not None:
                    self.cuckoo_delete_task(self.cuckoo_task.id)
                raise RecoverableError("Unable to retrieve cuckoo report. The following errors were detected: %s" %
                                       safe_str(self.cuckoo_task.errors))

        except Exception as e:
            # Delete the task now..
            self.log.info('General exception caught during processing: %s' % e)
            if self.cuckoo_task and self.cuckoo_task.id is not None:
                self.cuckoo_delete_task(self.cuckoo_task.id)

            # Send the exception off to ServiceBase
            raise

        # Delete and exit
        if self.cuckoo_task and self.cuckoo_task.id is not None:
            self.cuckoo_delete_task(self.cuckoo_task.id)

    @retry(wait_fixed=1000, retry_on_exception=_exclude_chain_ex,
           stop_max_attempt_number=3)
    def cuckoo_submit(self, file_content):
        try:
            """ Submits a new file to Cuckoo for analysis """
            task_id = self.cuckoo_submit_file(file_content)
            self.log.debug("Submitted file. Task id: %s.", task_id)
            if not task_id:
                err_msg = "Failed to get task for submitted file."
                self.cuckoo_task.errors.append(err_msg)
                self.log.error(err_msg)
                return
            else:
                self.cuckoo_task.id = task_id
        except Exception as e:
            err_msg = "Error submitting to Cuckoo"
            self.cuckoo_task.errors.append('%s: %s' % (err_msg, safe_str(e)))
            if self.cuckoo_task and self.cuckoo_task.id is not None:
                self.cuckoo_delete_task(self.cuckoo_task.id)
            raise RecoverableError("Unable to submit to Cuckoo")

        self.log.debug("Submission succeeded. File: %s -- Task ID: %s" % (self.cuckoo_task.file, self.cuckoo_task.id))

        try:
            status = self.cuckoo_poll_started()
        except RetryError:
            self.log.info("VM startup timed out")
            status = None

        if status == "started":
            try:
                status = self.cuckoo_poll_report()
            except RetryError:
                self.log.info("Max retries exceeded for report status.")
                status = None

        err_msg = None
        if status is None:
            err_msg = "Timed out while waiting for cuckoo to analyze file."
        elif status == "missing":
            err_msg = "Task went missing while waiting for cuckoo to analyze file."
        elif status == "stopped":
            err_msg = "Service has been stopped while waiting for cuckoo to analyze file."
        elif status == "missing_report":
            # this most often happens due to some sort of messed up filename that
            # the cuckoo agent inside the VM died on.
            new_filename = generate_random_words(1)
            file_ext = self.cuckoo_task.file.rsplit(".", 1)[-1]
            self.cuckoo_task.file = new_filename + "." + file_ext
            self.log.warning("Got missing_report status. This is often caused by invalid filenames. "
                             "Renaming file to %s and retrying" % self.cuckoo_task.file)
            # Raise an exception to force a retry
            raise Exception("Retrying after missing_report status")

        if err_msg:
            self.log.error(err_msg)
            if self.cuckoo_task and self.cuckoo_task.id is not None:
                self.cuckoo_delete_task(self.cuckoo_task.id)
            raise RecoverableError(err_msg)

    def stop(self):
        # Need to kill the container; we're about to go down..
        self.log.info("Service is being stopped; removing all running containers and metadata..")

    @retry(wait_fixed=1000,
           stop_max_attempt_number=GUEST_VM_START_TIMEOUT,
           retry_on_result=_retry_on_none)
    def cuckoo_poll_started(self):
        task_info = self.cuckoo_query_task(self.cuckoo_task.id)
        if task_info is None:
            # The API didn't return a task..
            return "missing"

        # Detect if mismatch
        if task_info.get("id") != self.cuckoo_task.id:
            self.log.warning("Cuckoo returned mismatched task info for task: %s. Trying again.." %
                             self.cuckoo_task.id)
            return None

        if task_info.get("guest", {}).get("status") == "starting":
            return None

        return "started"

    @retry(wait_fixed=CUCKOO_POLL_DELAY * 1000,
           retry_on_result=_retry_on_none,
           retry_on_exception=_exclude_chain_ex)
    def cuckoo_poll_report(self):
        task_info = self.cuckoo_query_task(self.cuckoo_task.id)
        if task_info is None or task_info == {}:
            # The API didn't return a task..
            return "missing"

        # Detect if mismatch
        if task_info.get("id") != self.cuckoo_task.id:
            self.log.warning("Cuckoo returned mismatched task info for task: %s. Trying again.." %
                             self.cuckoo_task.id)
            return None

        # Check for errors first to avoid parsing exceptions
        status = task_info.get("status")
        if "fail" in status:
            self.log.error("Analysis has failed. Check cuckoo server logs for errors.")
            self.cuckoo_task.errors = self.cuckoo_task.errors + task_info.get('errors')
            return status
        elif status == "completed":
            self.log.debug("Analysis has completed, waiting on report to be produced.")
        elif status == "reported":
            self.log.debug("Cuckoo report generation has completed.")

            try:
                self.cuckoo_task.report = self.cuckoo_query_report(self.cuckoo_task.id)
            except MissingCuckooReportException as e:
                return "missing_report"
            if self.cuckoo_task.report and isinstance(self.cuckoo_task.report, dict):
                return status
        else:
            self.log.debug("Waiting for task %d to finish. Current status: %s." % (self.cuckoo_task.id, status))

        return None

    @retry(wait_fixed=2000, stop_max_attempt_number=3)
    def cuckoo_submit_file(self, file_content):
        self.log.debug("Submitting file: %s to server %s" % (self.cuckoo_task.file, self.submit_url))
        files = {"file": (self.cuckoo_task.file, file_content)}
        try:
            resp = self.session.post(self.submit_url, files=files, data=self.cuckoo_task, headers=self.auth_header)
        except requests.exceptions.Timeout:
            if self.cuckoo_task and self.cuckoo_task.id is not None:
                self.cuckoo_delete_task(self.cuckoo_task.id)
            raise Exception("Cuckoo timed out after while trying to submit a file %s" % self.cuckoo_task.file)
        except requests.ConnectionError:
            if self.cuckoo_task and self.cuckoo_task.id is not None:
                self.cuckoo_delete_task(self.cuckoo_task.id)
            raise RecoverableError("Unable to reach the Cuckoo nest while trying to submit a file %s"
                                   % self.cuckoo_task.file)
        if resp.status_code != 200:
            self.log.debug("Failed to submit file %s. Status code: %s" % (self.cuckoo_task.file, resp.status_code))

            if resp.status_code == 500:
                new_filename = generate_random_words(1)
                file_ext = self.cuckoo_task.file.rsplit(".", 1)[-1]
                self.cuckoo_task.file = new_filename + "." + file_ext
                self.log.warning("Got 500 error from Cuckoo API. This is often caused by non-ascii filenames. "
                                 "Renaming file to %s and retrying" % self.cuckoo_task.file)
                # Raise an exception to force a retry
                raise Exception("Retrying after 500 error")
            return None
        else:
            resp_dict = dict(resp.json())
            task_id = resp_dict.get("task_id")
            if not task_id:
                # Spender case?
                task_id = resp_dict.get("task_ids", [])
                if isinstance(task_id, list) and len(task_id) > 0:
                    task_id = task_id[0]
                else:
                    return None
            return task_id

    @retry(wait_fixed=1000, stop_max_attempt_number=5,
           retry_on_exception=lambda x: not isinstance(x, MissingCuckooReportException))
    def cuckoo_query_report(self, task_id, fmt="json", params=None):
        self.log.debug("Querying report, task_id: %s - format: %s", task_id, fmt)
        try:
            resp = self.session.get(self.query_report_url % task_id + '/' + fmt, params=params or {},
                                    headers=self.auth_header)
        except requests.exceptions.Timeout:
            if self.cuckoo_task and self.cuckoo_task.id is not None:
                self.cuckoo_delete_task(self.cuckoo_task.id)
            raise Exception("Cuckoo timed out after while trying to query the report for task %s" % task_id)
        except requests.ConnectionError:
            raise RecoverableError("Unable to reach the Cuckoo nest while trying to query the report for task %s"
                                   % task_id)
        if resp.status_code != 200:
            if resp.status_code == 404:
                self.log.error("Task or report not found for task %s." % task_id)
                # most common cause of getting to here seems to be odd/non-ascii filenames, where the cuckoo agent
                # inside the VM dies
                if self.cuckoo_task and self.cuckoo_task.id is not None:
                    self.cuckoo_delete_task(self.cuckoo_task.id)
                raise MissingCuckooReportException("Task or report not found")
            else:
                self.log.error("Failed to query report %s. Status code: %d" % (task_id, resp.status_code))
                self.log.error(resp.text)
                return None
        if fmt == "json":
            try:
                # Setting environment recursion limit for large JSONs
                sys.setrecursionlimit(self.cfg['recursion_limit'])
                resp_dict = dict(resp.json())
                report_data = resp_dict
            except Exception:
                url = self.query_report_url % task_id + '/' + fmt
                self.log.exception("Exception converting cuckoo report http response into json: report url: %s, file_name: %s", url, self.file_name)
        else:
            report_data = resp.content

        if not report_data or report_data == '':
            if self.cuckoo_task and self.cuckoo_task.id is not None:
                self.cuckoo_delete_task(self.cuckoo_task.id)
            raise Exception("Empty report data")

        return report_data

    @retry(wait_fixed=2000)
    def cuckoo_query_pcap(self, task_id):
        try:
            resp = self.session.get(self.query_pcap_url % task_id, headers=self.auth_header)
        except requests.exceptions.Timeout:
            if self.cuckoo_task and self.cuckoo_task.id is not None:
                self.cuckoo_delete_task(self.cuckoo_task.id)
            raise Exception("Cuckoo timed out after while trying to query the pcap for task %s" % task_id)
        except requests.ConnectionError:
            raise RecoverableError("Unable to reach the Cuckoo nest while trying to query the pcap for task %s"
                                   % task_id)
        pcap_data = None
        if resp.status_code != 200:
            if resp.status_code == 404:
                self.log.debug("Task or pcap not found for task: %s" % task_id)
            else:
                self.log.debug("Failed to query pcap for task %s. Status code: %d" % (task_id, resp.status_code))
        else:
            pcap_data = resp.content
        return pcap_data

    @retry(wait_fixed=500, stop_max_attempt_number=3, retry_on_result=_retry_on_none)
    def cuckoo_query_task(self, task_id):
        try:
            resp = self.session.get(self.query_task_url % task_id, headers=self.auth_header)
        except requests.exceptions.Timeout:
            if self.cuckoo_task and self.cuckoo_task.id is not None:
                self.cuckoo_delete_task(self.cuckoo_task.id)
            raise Exception("Cuckoo timed out after while trying to query the task %s" % task_id)
        except requests.ConnectionError:
            raise RecoverableError("Unable to reach the Cuckoo nest while trying to query the task %s" % task_id)
        task_dict = None
        if resp.status_code != 200:
            if resp.status_code == 404:
                self.log.debug("Task not found for task: %s" % task_id)
            else:
                self.log.debug("Failed to query task %s. Status code: %d" % (task_id, resp.status_code))
        else:
            resp_dict = dict(resp.json())
            task_dict = resp_dict.get('task')
            if task_dict is None or task_dict == '':
                self.log.warning('Failed to query task. Returned task dictionary is None or empty')
        return task_dict

    @retry(wait_fixed=2000)
    def cuckoo_query_machine_info(self, machine_name):
        try:
            resp = self.session.get(self.query_machine_info_url % machine_name, headers=self.auth_header)
        except requests.exceptions.Timeout:
            if self.cuckoo_task and self.cuckoo_task.id is not None:
                self.cuckoo_delete_task(self.cuckoo_task.id)
            raise Exception("Cuckoo timed out after while trying to query machine info for %s" % machine_name)
        except requests.ConnectionError:
            raise RecoverableError("Unable to reach the Cuckoo nest while trying to query machine info for %s"
                                   % machine_name)
        machine_dict = None
        if resp.status_code != 200:
            self.log.debug("Failed to query machine %s. Status code: %d" % (machine_name, resp.status_code))
        else:
            resp_dict = dict(resp.json())
            machine_dict = resp_dict.get('machine')
        return machine_dict

    @retry(wait_fixed=1000, stop_max_attempt_number=2)
    def cuckoo_delete_task(self, task_id):
        try:
            resp = self.session.get(self.delete_task_url % task_id, headers=self.auth_header)
        except requests.exceptions.Timeout:
            raise Exception("Cuckoo timed out after while trying to delete task %s" % task_id)
        except requests.ConnectionError:
            raise RecoverableError("Unable to reach the Cuckoo nest while trying to delete task %s" % task_id)
        if resp.status_code != 200:
            self.log.debug("Failed to delete task %s. Status code: %d" % (task_id, resp.status_code))
        else:
            self.log.debug("Deleted task: %s." % task_id)
            if self.cuckoo_task:
                self.cuckoo_task.id = None

    # Fixed retry amount to avoid starting an analysis too late.
    @retry(wait_fixed=5000, stop_max_attempt_number=6)
    def cuckoo_query_machines(self):
        self.log.debug("Querying for available analysis machines using url %s.." % self.query_machines_url)
        try:
            resp = self.session.get(self.query_machines_url, headers=self.auth_header)
        except requests.exceptions.Timeout:
            raise Exception("Cuckoo timed out after while trying to query machines")
        except requests.ConnectionError:
            raise RecoverableError("Unable to reach the Cuckoo nest while trying to query machines")
        if resp.status_code != 200:
            self.log.debug("Failed to query machines: %s" % resp.status_code)
            raise CuckooVMBusyException()
        resp_dict = dict(resp.json())
        return resp_dict

    def check_dropped(self, request, task_id):
        self.log.debug("Checking dropped files.")
        dropped_tar_bytes = self.cuckoo_query_report(task_id, 'dropped')
        added_hashes = set()
        if dropped_tar_bytes is not None:
            try:
                dropped_tar = tarfile.open(fileobj=io.BytesIO(dropped_tar_bytes))
                for tarobj in dropped_tar:
                    if tarobj.isfile() and not tarobj.isdir():  # a file, not a dir
                        # A dropped file found
                        dropped_name = os.path.split(tarobj.name)[1]
                        # Fixup the name.. the tar originally has files/your/file/path
                        tarobj.name = tarobj.name.replace("/", "_").split('_', 1)[1]
                        dropped_tar.extract(tarobj, self.working_directory)
                        dropped_file_path = os.path.join(self.working_directory, tarobj.name)

                        # Check the file hash for whitelisting:
                        with open(dropped_file_path, 'rb') as file_hash:
                            data = file_hash.read()
                            if not request.task.deep_scan:
                                ssdeep_hash = ssdeep.hash(data)
                                skip_file = False
                                for seen_hash in added_hashes:
                                    if ssdeep.compare(ssdeep_hash, seen_hash) >= self.ssdeep_match_pct:
                                        skip_file = True
                                        break
                                if skip_file is True:
                                    dropped_sec = ResultSection(title_text='Dropped Files Information',
                                                                classification=self.SERVICE_CLASSIFICATION)
                                    dropped_sec.add_tag("file.behavior",
                                                        "Truncated extraction set")
                                    continue
                                else:
                                    added_hashes.add(ssdeep_hash)
                            dropped_hash = hashlib.md5(data).hexdigest()
                            if dropped_hash == self.task.md5:
                                continue
                        if not (wlist_check_hash(dropped_hash) or wlist_check_dropped(
                                dropped_name) or dropped_name.endswith('_info.txt')):
                            # Resubmit
                            self.task.add_extracted(dropped_file_path,
                                                    dropped_name,
                                                    "Dropped file during Cuckoo analysis.")
                            self.log.debug("Submitted dropped file for analysis: %s" % dropped_file_path)
            except Exception as e_x:
                self.log.error("Error extracting dropped files: %s" % e_x)
                return

    def get_errors(self):
        # Return errors from our sections
        # TODO: This is a bit (REALLY) hacky, we should probably flag this during result generation.
        for section in self.file_res.sections:
            if section.title_text == "Analysis Errors":
                return section.body
        return ""

    def check_pcap(self, task_id):
        # Make sure there's actual network information to report before including the pcap.
        # TODO: This is also a bit (REALLY) hacky, we should probably flag this during result generation.
        has_network = False
        for section in self.file_res.sections:
            if section.title_text == "Network Activity":
                has_network = True
                break
        if not has_network:
            return

        pcap_data = self.cuckoo_query_pcap(task_id)
        if pcap_data:
            pcap_file_name = "cuckoo_traffic.pcap"
            pcap_path = os.path.join(self.working_directory, pcap_file_name)
            pcap_file = open(pcap_path, 'wb')
            pcap_file.write(pcap_data)
            pcap_file.close()

            # Resubmit analysis pcap file
            try:
                self.task.add_extracted(pcap_path, pcap_file_name, "PCAP from Cuckoo analysis")
            except MaxExtractedExceeded:
                self.log.debug("The maximum amount of files to be extracted is 501, "
                               "which has been exceeded in this submission")

    def report_machine_info(self, machine_name):
        try:
            self.log.debug("Querying machine info for %s" % machine_name)
            machine_name_exists = False
            machine = None
            for machine in self.machines.get('machines'):
                if machine.get('name') == machine_name:
                    machine_name_exists = True
                    break

            if not machine_name_exists:
                raise Exception

            body = {
                'id': str(machine.get('id')),
                'name': str(machine.get('name')),
                'label': str(machine.get('label')),
                'platform': str(machine.get('platform')),
                'tags': []}
            for tag in machine.get('tags', []):
                body['tags'].append(safe_str(tag).replace('_', ' '))

            machine_section = ResultSection(title_text='Machine Information',
                                            classification=self.SERVICE_CLASSIFICATION,
                                            body_format=BODY_FORMAT.KEY_VALUE,
                                            body=json.dumps(body))

            self.file_res.add_section(machine_section)
            return str(machine.get('ip', ""))
        except Exception as exc:
            self.log.error('Unable to retrieve machine information for %s: %s' % (machine_name, safe_str(exc)))


def generate_random_words(num_words):
    alpha_nums = [chr(x + 65) for x in range(26)] + [chr(x + 97) for x in range(26)] + [str(x) for x in range(10)]
    return " ".join(["".join([random.choice(alpha_nums)
                              for _ in range(int(random.random() * 10) + 2)])
                     for _ in range(num_words)])