import datetime
import hashlib
import logging
import re
import ssdeep
import traceback
import json
import os

from collections import defaultdict
from pprint import pprint

from assemblyline_v4_service.common.task import MaxExtractedExceeded
from assemblyline.common.str_utils import safe_str
from assemblyline_v4_service.common.result import Result, BODY_FORMAT, ResultSection, Classification, InvalidClassification
from cuckoo.clsids import clsids
from cuckoo.whitelist import wlist_check_ip, wlist_check_domain, wlist_check_hash
from cuckoo.signatures import check_signature

try:
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from assemblyline.al.service.base import ServiceRequest
except ImportError:
    # we don't care if this isn't here at runtime...
    pass

UUID_RE = re.compile(r"{([0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12})\}")
USER_SID_RE = re.compile(r"S-1-5-21-\d+-\d+-\d+-\d+")
WIN_FILE_RE = re.compile(r"Added new file to list with path: (\w:(?:\\[a-zA-Z0-9_\-. $]+)+)")
DROIDMON_CONN_RE = re.compile(r"([A-Z]{3,5}) (https?://([a-zA-Z0-9.\-]+):?([0-9]{2,5})?([^ ]+)) HTTP/([0-9.]+)")
log = logging.getLogger('assemblyline.svc.cuckoo.cuckooresult')


# noinspection PyBroadException
def generate_al_result(api_report, al_result, al_request, file_ext, guest_ip, service_classification=Classification.UNRESTRICTED):
    log.debug("Generating AL Result.")
    try:
        classification = Classification.max_classification(Classification.UNRESTRICTED, service_classification)
    except InvalidClassification as e:
        log.warning("Could not get the service classification: %s" % e.message)
        return False

    info = api_report.get('info')
    if info is not None:
        start_time = info.get('started')
        end_time = info.get('ended')
        try:
            start_time = datetime.datetime.fromtimestamp(int(start_time)).strftime('%Y-%m-%d %H:%M:%S')
            end_time = datetime.datetime.fromtimestamp(int(end_time)).strftime('%Y-%m-%d %H:%M:%S')
        except:
            pass
        body = {
            'Cuckoo Version': info.get('version'),
            'Analysis ID': info.get('id'),
            'Analysis Duration': info.get('duration'),
            'Start Time': start_time,
            'End Time': end_time
        }
        info_res = ResultSection(title_text='Analysis Information',
                                 classification=classification,
                                 body_format=BODY_FORMAT.KEY_VALUE,
                                 body=json.dumps(body))
        al_result.add_section(info_res)

    behavior = api_report.get('behavior')
    network = api_report.get('network', {})
    droidmon = api_report.get('droidmon')
    debug = api_report.get('debug')
    sigs = api_report.get('signatures', [])

    executed = True
    if debug:
        process_debug(debug, al_result, classification)

    if behavior:
        executed = process_behavior(behavior, al_result, al_request, classification)

    if droidmon:
        process_droidmon(droidmon, network, al_result, classification)

    if executed is True:
        if network:
            process_network(network, al_result, guest_ip, classification)
        if sigs:
            process_signatures(sigs, al_result, classification)
    else:
        log.debug("It doesn't look like this file executed (unsupported file type?)")
        noexec_res = ResultSection(title_text="Notes", classification=classification)
        noexec_res.add_line('Unrecognized file type: '
                            'No program available to execute a file with the following extension: %s'
                            % file_ext)
        al_result.add_section(noexec_res)
    log.debug("AL result generation completed!")
    return True


def process_clsid(key, result_map):
    clsid_map = result_map.get('clsids', defaultdict(str))
    for uuid in set(UUID_RE.findall(safe_str(key))):
        # Check if we have a matching CLSID
        uuid = uuid.upper()
        name = clsids.get(uuid)
        if name:
            clsid_map[name] = uuid
    result_map['clsids'] = clsid_map


def process_droidmon(droidmon, network, al_result, classification):
    droidmon_res = ResultSection(title_text="Droidmon", classification=classification)

    if 'raw' in droidmon:
        classes = set()
        for raw_entry in droidmon['raw']:
            if "class" in raw_entry:
                classes.add(raw_entry['class'])
        if len(classes) > 0:
            sorted_classes = sorted(safe_str(x) for x in classes)
            _, cls_hash_one, cls_hash_two = ssdeep.hash(''.join(sorted_classes)).split(':')
            droidmon_res.add_tag("dynamic.ssdeep.dynamic_classes", cls_hash_one)
            droidmon_res.add_tag("dynamic.ssdeep.dynamic_classes", cls_hash_two)
    if 'httpConnections' in droidmon:
        # Add this http information to the main network map:
        for req in droidmon['httpConnections']:
            match = DROIDMON_CONN_RE.match(req["request"])
            if match:
                meth = match.group(1)
                uri = match.group(2)
                domain = match.group(3)
                port = match.group(4)
                path = match.group(5)
                ver = match.group(6)
                seen = False
                for entry in network['http']:
                    if entry['uri'] == uri and entry['method'] == meth and entry['port'] == port:
                        entry['count'] += 1
                        seen = True
                        break
                if not seen:
                    new_entry = {
                        "count": 1,
                        "body": "",
                        "uri": uri,
                        "user-agent": "",
                        "method": meth,
                        "host": domain,
                        "version": ver,
                        "path": path,
                        "data": "",
                        "port": int(port) if port else None
                    }
                    log.warning(new_entry)
                    network['http'].append(new_entry)

    if 'sms' in droidmon:
        sms_res = ResultSection(title_text='SMS Activity',
                                classification=classification,
                                body_format=BODY_FORMAT.MEMORY_DUMP)
        sms_res.set_heuristic(1)
        sms_lines = dict_list_to_fixedwidth_str_list(droidmon['sms'])
        for sms_line in sms_lines:
            sms_res.add_line(sms_line)
        for sms in droidmon['sms']:
            droidmon_res.add_tag("info.phone_number", sms['dest_number'])
        al_result.add_section(sms_res)

    if 'crypto_keys' in droidmon:
        crypto_res = ResultSection(title_text='Crypto Keys',
                                   classification=classification,
                                   body_format=BODY_FORMAT.MEMORY_DUMP)
        crypto_res.set_heuristic(2)
        crypto_key_lines = dict_list_to_fixedwidth_str_list(droidmon['crypto_keys'])
        for crypto_key_line in crypto_key_lines:
            crypto_res.add_line(crypto_key_line)
        for crypto_key in droidmon['crypto_keys']:
            droidmon_res.add_tag("technique.crypto", crypto_key['type'])
        al_result.add_section(crypto_res)


def process_debug(debug, al_result, classification):
    failed = False
    if 'errors' in debug:
        error_res = ResultSection(title_text='Analysis Errors', classification=classification)
        for error in debug['errors']:
            err_str = str(error)
            err_str = err_str.lower()
            if err_str is not None and len(err_str) > 0:
                # Timeouts - ok, just means the process never exited
                # Start Error - probably a corrupt file..
                # Initialization Error - restart the docker container
                error_res.add_line(error)
                # if "analysis hit the critical timeout" not in err_str and \
                #     "Unable to execute the initial process" not in err_str:
                #     raise RecoverableError("An error prevented cuckoo from "
                #                            "generating complete results: %s" % safe_str(error))
        if error_res.body and len(error_res.body) > 0:
            al_result.add_section(error_res)
    return failed


def process_key(key, result_map):
    keys = result_map.get('regkeys', [])
    key = USER_SID_RE.sub("S-1-5-21-<DOMAIN_ID>-<RELATIVE_ID>", key)
    keys.append(key)
    keys.append(key)
    # Check for CLSIDs
    process_clsid(key, result_map)
    result_map['regkeys'] = keys


def process_com(args, result_map):
    if "clsid" in args:
        process_clsid(args.get("clsid"), result_map)
    else:
        for arg in args:
            if isinstance(arg, dict):
                if arg.get("name") == "ClsId":
                    process_clsid(arg.get("value"), result_map)
            elif isinstance(arg, str):
                process_clsid(arg, result_map)


def process_behavior(behavior, al_result, al_request, classification):
    # type: (dict, Result, ServiceRequest, str) -> bool
    log.debug("Processing behavior results.")
    executed = True
    result_map = {}
    res_sec = None

    # Spender
    for key in behavior.get("summary", {}).get("keys", []):
        process_key(key, result_map)
    # Cuckoobox
    for key in behavior.get("summary", {}).get("regkey_opened", []):
        process_key(key, result_map)

    result_map['processtree'] = behavior.get("processtree")
    for process in behavior.get("processes"):
        # pid = process.get("process_id")
        for call in process.get("calls"):
            api = call.get("api")
            if "CoCreateInstance" in api:
                process_com(call.get("arguments"), result_map)
                # TODO: More interesting API stuff.

    guids = behavior.get("summary", {}).get("guid", [])

    result_limit = 25

    result_queries = {"directory_created":  ["Directories Created", result_limit, None],
                      "directory_removed":  ["Directories Deleted", result_limit, None],
                      "dll_loaded":         ["Modules Loaded", result_limit, None],
                      "file_deleted":       ["Files Deleted", result_limit, None],
                      "file_exists":        ["Check File: Exists", result_limit, None],
                      "file_failed":        ["Check File: Failed", result_limit, None],
                      "regkey_written":     ["Registry Keys Written", result_limit, None],
                      "command_line":       ["Commands", None, None],
                      "downloads_file":     ["Files Downloads", None, None],
                      "file_written":       ["Files Written", None, "file.path"],
                      "wmi_query":          ["WMI Queries", None, None],
                      "mutex":              ["Mutexes", None, "dynamic.mutex"],
                      }
    for q_name, [title, limit, tag_type] in result_queries.items():
        q_res = behavior.get("summary", {}).get(q_name, [])
        if q_res:
            if limit is not None:
                q_res = q_res[:limit]
                title = "%s (Limit %i)" % (title, limit)

            res_sec = ResultSection(title_text=title, classification=classification)
            for ln in map(safe_str, q_res):
                res_sec.add_line(ln)
                if tag_type is not None:
                    res_sec.add_tag(tag_type, ln)
            # Dump out contents to a temporary file and add as an extracted file
            if q_name == "command_line":
                for raw_ln in q_res:
                    cli_hash = hashlib.sha256(raw_ln.encode('utf-8')).hexdigest()
                    temp_filepath = os.path.join(al_request._working_directory, "command_%s" % cli_hash[:10])
                    with open(temp_filepath, 'wb') as temp_fh:
                        temp_fh.write(raw_ln.encode())
                    try:
                        al_request.add_extracted(temp_filepath, "command_line_dump.txt",
                                                 "Extracted command_line from Cuckoo")
                    except MaxExtractedExceeded:
                        log.debug("The maximum amount of files to be extracted is 501, "
                                  "which has been exceeded in this submission")
            al_result.add_section(res_sec)

    if len(guids) > 0:
        process_com(guids, result_map)

    # Make it serializable and sorted.. maybe we hash these?
    # Could probably do the same thing with registry keys..
    if result_map.get('clsids', {}) != {}:
        # Hash
        sorted_clsids = sorted([safe_str(x) for x in result_map['clsids'].values()])
        ssdeep_clsid_hash = ssdeep.hash(''.join(sorted_clsids))
        res_sec.add_tag("dynamic.ssdeep.cls_ids", ssdeep_clsid_hash)

        clsids_hash = hashlib.sha1((','.join(sorted_clsids)).encode('utf-8')).hexdigest()
        if wlist_check_hash(clsids_hash):
            # Benign activity
            executed = False

        # Report
        clsid_res = ResultSection(title_text="CLSIDs", classification=classification)
        for clsid in sorted(result_map['clsids'].keys()):
            clsid_res.add_line(clsid + ' : ' + result_map['clsids'][clsid])
        al_result.add_section(clsid_res)

    if len(result_map.get('regkeys', [])) > 0:
        sorted_regkeys = sorted([safe_str(x) for x in result_map['regkeys']])
        regkey_hash = ssdeep.hash(''.join(sorted_regkeys))
        res_sec.add_tag("dynamic.ssdeep.regkeys", value=regkey_hash)

    log.debug("Behavior processing completed. Looks like valid execution: %s" % str(executed))
    return executed


def process_signatures(sigs, al_result, classification):
    log.debug("Processing signature results.")
    if len(sigs) > 0:
        sigs_res = ResultSection(title_text="Signatures", classification=classification)
        skipped_sigs = ['dead_host', 'has_authenticode', 'network_icmp', 'network_http', 'allocates_rwx', 'has_pdb']
        print_iocs = ['dropper', 'suspicious_write_exe', 'suspicious_process', 'uses_windows_utilities',
                      'persistence_autorun']
        # Severity is 0-5ish with 0 being least severe.
        for sig in sigs:
            sig_name = sig.get('name')

            # Skipped Signature Checks:
            if sig_name in skipped_sigs:
                continue

            sig_id = check_signature(sig_name)
            if sig_id == 3:
                log.warning("Unknown signature detected: %s" % sig)
            actor = sig.get('actor', '')
            description = sig.get('description', '')
            title_text = "Signature: " + sig_name
            sig_res = ResultSection(title_text=title_text, classification=classification, body=description)
            sig_res.set_heuristic(sig_id)
            sigs_res.add_subsection(sig_res)
            sig_categories = sig.get('categories', [])
            sig_families = sig.get('families', [])
            sig_marks = sig.get('marks', [])

            if len(sig_categories) > 0:
                sigs_res.add_line('\tCategories: ' + ','.join([safe_str(x) for x in sig_categories]))
                for category in sig_categories:
                    sigs_res.add_tag("dynamic.signature.category", category)

            if len(sig_families) > 0:
                sigs_res.add_line('\tFamilies: ' + ','.join([safe_str(x) for x in sig_families]))
                for family in sig_families:
                    sigs_res.add_tag("dynamic.signature.category", family)

            if actor and actor != '':
                sigs_res.add_tag("attribution.actor", actor)
            if sig_name in print_iocs:
                for mark in sig_marks:
                    if mark.get('type') == 'ioc' and mark.get('category') in ['url', 'file', 'cmdline', 'request']:
                        sigs_res.add_line('\tIOC: %s' % mark['ioc'])
                    elif mark.get('type') == 'generic' and 'reg_key' in mark and 'reg_value' in mark:
                        sigs_res.add_line('\tIOC: %s = %s' % (mark['reg_key'], mark['reg_value']))

        al_result.add_section(sigs_res)


def parse_protocol_data(flow_data, group_by='dst', group_fields=list()):
    protocol_data = defaultdict(list)
    for flow in flow_data:
        group = flow.get(group_by)
        flow_data = {}
        for field in group_fields:
            flow_data[field] = flow.get(field)
        if flow_data not in protocol_data[group]:
            protocol_data[group].append(flow_data)
    return protocol_data


def dict_list_to_fixedwidth_str_list(dict_list, print_keys=True):
    out_lines = []
    lens = {}
    max_lens = {}
    for in_dict in dict_list:
        for k, v in in_dict.items():
            k_len = len(str(k))
            v_len = len(str(v))
            max_lens[k] = max(max_lens.get(k, 0), v_len+4)
            lens[k] = (k_len, max_lens[k])
    if print_keys:
        fmt_template = '{0:<%d}: {1:<%d}'
    else:
        fmt_template = '{0:<%d}'
    
    for in_dict in dict_list:
        output = ''
        for k in sorted(in_dict.keys()):
            if print_keys:
                fmt = fmt_template % lens[k]
                output += fmt.format(k, in_dict[k])
            else:
                fmt = fmt_template % lens[k][1]
                output += fmt.format(in_dict[k])
            
        out_lines.append(output)
    return out_lines


# This is probably just a temporary requirement.. the _ex http/s flow data doesn't have the same formatting
# for the uri field.
def _add_ex_data(proto_data, proto_ex_data, protocol, port):
    # Format and add _ex data
    for host in proto_ex_data:
        for flow in proto_ex_data[host]:
            if flow['dport'] == port:
                full_uri = "%s://%s%s" % (protocol, host, flow['uri'])
            else:
                full_uri = "%s://%s:%d%s" % (protocol, host, flow['dport'], flow['uri'])
            flow['uri'] = full_uri
            flow['port'] = flow['dport']
            flow.pop('dport')
        if host in proto_data:
            for flow in proto_ex_data[host]:
                if flow not in proto_data[host]:
                    proto_data[host].append(flow)
        else:
            proto_data[host] = proto_ex_data[host][:]


def process_network(network, al_result, guest_ip, classification):
    log.debug("Processing network results.")
    result_map = {}

    network_res = ResultSection(title_text="Network Activity",
                                classification=classification,
                                body_format=BODY_FORMAT.MEMORY_DUMP)
    # IP activity
    hosts = network.get("hosts", [])
    if len(hosts) > 0 and isinstance(hosts[0], dict):
        hosts = [host['ip'] for host in network.get("hosts", [])]

    udp = parse_protocol_data(network.get("udp", []), group_fields=['dport'])
    tcp = parse_protocol_data(network.get("tcp", []), group_fields=['dport'])
    smtp = parse_protocol_data(network.get("smtp", []), group_fields=['raw'])
    dns = parse_protocol_data(network.get("dns", []), group_by='request', group_fields=['answers'])
    icmp = parse_protocol_data(network.get("icmp", []), group_fields=['type'])

    # Domain activity
    domains = parse_protocol_data(network.get("domains", []), group_by='domain')

    http = parse_protocol_data(network.get("http", []), group_by='host',
                               group_fields=['port', 'uri', 'method'])
    http_ex = parse_protocol_data(network.get("http_ex", []), group_by='host',
                                  group_fields=['dport', 'uri', 'method'])
    _add_ex_data(http, http_ex, 'http', 80)

    https = parse_protocol_data(network.get("https", []), group_by='host',
                                group_fields=['port', 'uri', 'method'])
    https_ex = parse_protocol_data(network.get("https_ex", []), group_by='host',
                                   group_fields=['dport', 'uri', 'method'])
    _add_ex_data(https, https_ex, 'https', 443)

    # Miscellaneous activity
    # irc = network.get("irc")

    # Add missing ip hosts
    for proto in [udp, tcp, http, https, icmp, smtp]:
        for hst in proto.keys():
            if hst not in hosts and re.match(r"^[0-9.]+$", hst):
                hosts.append(hst)

    # network['hosts'] has all unique non-local network ips.
    for host in hosts:
        if host == guest_ip or wlist_check_ip(host):
            continue
        add_host_flows(host, 'udp', udp.get(host), result_map)
        add_host_flows(host, 'tcp', tcp.get(host), result_map)
        add_host_flows(host, 'smtp', smtp.get(host), result_map)
        add_host_flows(host, 'icmp', icmp.get(host), result_map)
        add_host_flows(host, 'http', http.get(host), result_map)
        add_host_flows(host, 'https', https.get(host), result_map)

    if hosts != [] and 'host_flows' not in result_map:
        # This only occurs if for some reason we don't parse corresponding flows out from the
        # network dump. So we'll just manually add the IPs so they're at least being reported.
        result_map['host_flows'] = {}
        for host in hosts:
            if host == guest_ip or wlist_check_ip(host):
                continue
            result_map['host_flows'][host] = []

    for domain in domains:
        if wlist_check_domain(domain):
            continue
        add_domain_flows(domain, 'dns', dns.get(domain), result_map)
        add_domain_flows(domain, 'http', http.get(domain), result_map)
        add_domain_flows(domain, 'https', https.get(domain), result_map)

    if 'host_flows' in result_map:
        # host_flows is a map of host:protocol entries
        # protocol is a map of protocol_name:flows
        # flows is a set of unique flows by the groupings above
        host_lines = []
        for host in sorted(result_map['host_flows']):
            protocols = result_map['host_flows'].get(host, [])
            host_cc = '??'
            host_cc = '('+host_cc+')'
            network_res.add_tag("network.ip", host)
            for protocol in sorted(protocols):
                flows = protocols[protocol]
                if 'http' in protocol:
                    for flow in flows:
                        uri = flow.get('uri', None)
                        if uri:
                            network_res.add_tag("network.uri", uri)
                flow_lines = dict_list_to_fixedwidth_str_list(flows)
                for line in flow_lines:
                    proto_line = "{0:<8}{1:<19}{2:<8}{3}".format(protocol, host, host_cc, line)
                    host_lines.append(proto_line)

        hosts_res = ResultSection(title_text='IP Flows', classification=classification, body=host_lines)
        hosts_res.set_heuristic(1001)
        network_res.add_subsection(hosts_res)

    if 'domain_flows' in result_map:
        # domain_flows is a map of domain:protocol entries
        # protocol is a map of protocol_name:flows
        # flows is a set of unique flows by the groupings above

        # Formatting..
        max_domain_len = 0
        for domain in result_map['domain_flows']:
            max_domain_len = max(max_domain_len, len(domain)+4)
        proto_fmt = "{0:<8}{1:<"+str(max_domain_len)+"}{2}"
        domain_lines = []
        for domain in sorted(result_map['domain_flows']):
            protocols = result_map['domain_flows'][domain]
            network_res.add_tag("network.domain", domain)
            for protocol in sorted(protocols):
                flows = protocols[protocol]
                flow_lines = None
                if 'http' in protocol:
                    for flow in flows:
                        uri = flow.get('uri', None)
                        if uri:
                            network_res.add_tag("network.uri", uri)
                    flow_lines = dict_list_to_fixedwidth_str_list(flows)
                if 'dns' in protocol:
                    for flow in flows:
                        answers = flow.get('answers', None)
                        if answers:
                            flow_lines = dict_list_to_fixedwidth_str_list(answers)
                for line in flow_lines:
                    proto_line = proto_fmt.format(protocol, domain, line)
                    domain_lines.append(proto_line)                
#                 domain_res.add_lines(protocol_lines)
#             domains_res.add_section(domain_res)
        domains_res = ResultSection(title_text='Domain Flows', classification=classification, body=domain_lines)
        domains_res.set_heuristic(1000)
        network_res.add_subsection(domains_res)
    al_result.add_section(network_res)
    log.debug("Network processing complete.")


def add_host_flows(host, protocol, flows, result_map):
    if flows is None:
        return
    host_flows = result_map.get('host_flows', defaultdict(dict))
    flow_key = host
    host_flows[flow_key][protocol] = flows
    result_map['host_flows'] = host_flows


def add_domain_flows(domain, protocol, flows, result_map):
    if flows is None:
        return
    domain_flows = result_map.get('domain_flows', defaultdict(dict))
    flow_key = domain
    domain_flows[flow_key][protocol] = flows
    result_map['domain_flows'] = domain_flows

#  TEST CODE
if __name__ == "__main__":
    import sys
    import json
    report_path = sys.argv[1]
    with open(report_path, 'r') as fh:
        data = json.loads(fh.read())
    res = Result()
    # noinspection PyBroadException
    try:
        generate_al_result(data, res, '.js', Classification.UNRESTRICTED)
    except Exception:
        traceback.print_exc()
    pprint(res)
