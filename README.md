# CUCKOO DEPLOYMENT INSTRUCTIONS

Prior to provisioning a Cuckoo service, please read and understand this document. Failure to do so may result in a 
large volume of error messages in your hostagent log file. 

## Configurations

The Cuckoo service provides a number of sane default configurations. However, if the user plans on running multiple 
virtual machines simultaneously, two options should change.

| Name | Default | Description |
|:---:|:---:|---|
|ramdisk_size|2048M|This is the size of the ramdisk that Cuckoo will use to store VM snapshots and the running virtual machine image. If it's not large enough analysis will fail, see the Troubleshooting section for more information.|
|ram_limit|3072m|This is the maximum amount of ram usable by the Cuckoobox docker container. It doesn't include memory used by inetsim or the Cuckoo service. It should be at least 1G greater than the ramdisk.|


## DOCKER COMPONENTS

### Registry

Refer to the following website for registry deployment options.

    https://docs.docker.com/registry/deploying/

To simply start up a local registry, run the following commands

    sudo docker run -d -p 5000:5000 --name registry registry:2

Make sure to configure this registry in ASSEMBLYLINE.

    installation -> docker -> private_registry = 'localhost:5000'

### Build Docker Images

The following commands assume a local registry. Change localhost as needed for a remote registry. If a remote registry 
is configured on all workers, the following commands will only need to be run once.

    cd /opt/al/pkg/assemblyline/al/service/cuckoo/docker/kvm
    sudo docker build -t cuckoo/kvm .
    cd ../cuckoobox
    sudo apt-get install python-dev libffi-dev libfuzzy-dev
    sudo -u al bash libs.sh
    sudo docker build -t localhost:5000/cuckoo/cuckoobox .
    sudo docker push localhost:5000/cuckoo/cuckoobox
    cd ../inetsim
    sudo docker build -t localhost:5000/cuckoo/inetsim .
    sudo docker push localhost:5000/cuckoo/inetsim
    cd ../gateway
    sudo docker build -t localhost:5000/cuckoo/gateway .
    sudo docker push localhost:5000/cuckoo/gateway

### Additional Routes

By default Cuckoo ships with two routes for network traffic. The internet simulator "inetsim", and "gateway," a direct 
connection to the internet via the ASSEMBLYLINE worker's gateway. Additional docker containers, in support of a VPN for 
example, can be added. Make sure to update the enabled_routes variable in Cuckoo's service configuration.

## EPHEMERAL VM

### Build Base Virtual Machine

This step will very slightly depending on whatever operating system you choose. These are examples for Windows 7 and 
Ubuntu. Cuckoo expects all virtual machine data and metadata to exist under /opt/al/var/support/vm/disks/cuckoo/ 
which can be modified via the ASSEMBLYLINE configurations.

Before continuing, make sure the following libraries are installed:

    sudo apt-get install libguestfs-tools python-guestfs

#### Ubuntu 14.04

    sudo -u al mkdir -p /opt/al/var/support/vm/disks/cuckoo/Ubuntu1404/
    sudo -u al qemu-img create -f qcow2 /opt/al/var/support/vm/disks/cuckoo/Ubuntu1404/Ub14disk.qcow2 20G
    sudo virt-install --connect qemu:///system --virt-type kvm --name Ubuntu1404 --ram 1024             \
        --disk path=/opt/al/var/support/vm/disks/cuckoo/Ubuntu1404/Ub14disk.qcow2,size=20,format=qcow2  \
        --vnc --cdrom /path/to/install/CD.iso  --network network=default,mac=00:01:02:16:32:63          \
        --os-variant ubuntutrusty
        
Once the operating system has been installed, perform the following setup.

* Set NOPASSWD on the user accounts sudoers entry
* Set the user account to automatically login
* Copy agent.py from the cuckoo repository to the main users home directory in the virtual machine
* Set `sudo ~/agent.py` and `bash /bootstrap.sh` to run on login
  * This step will depend on window manager, but the command `gnome-session-manager` works for gnome
* Install the following packages on the virtual machine: systemtap, gcc, linux-headers-$(uname -r)
* Copy `data/strace.stp` onto the virtual machine
* Run `sudo stap -k 4 -r $(uname -r) strace.stp -m stap_ -v`
* Place stap_.ko into /root/.cuckoo/

When done, shutdown the virtual machine. Remove the CD drive configuration from the virtual machine. The virtual 
machine will fail if it contains any references to the install medium.

    sudo virsh edit Ubuntu1404

Create a snapshot of the virtual machine.

    sudo virsh snapshot-create Ubuntu1404

Verify that there is a "current" snapshot with the following command, it should result in a lot of XML.

    sudo virsh snapshot-current Ubuntu1404

Then continue from the "Prepare the snapshot for Cuckoo" section.

#### Windows 7

    sudo -u al mkdir -p /opt/al/var/support/vm/disks/cuckoo/Win7SP1x86/
    sudo -u al qemu-img create -f qcow2 /opt/al/var/support/vm/disks/cuckoo/Win7SP1x86/Win7disk.qcow2 20G
    sudo virt-install --connect qemu:///system --virt-type kvm --name Win7SP1x86 --ram 1024             \
        --disk path=/opt/al/var/support/vm/disks/cuckoo/Win7SP1x86/Win7disk.qcow2,size=20,format=qcow2  \
        --vnc --cdrom /path/to/install/CD.iso  --network network=default,mac=00:01:02:16:32:64          \
        --os-variant win7 --video cirrus

Once the operating system has been installed, perform the following setup.

* Install Python 2.7
* Optional: Install PIL (Python Image Library) if periodic screenshots are desired
* Disable Windows Update, Windows Firewall, and UAC(User Access Control)
* set python.exe and pythonw.exe to "Run as Administrator"
* Optional: Install Java, .Net, and other runtime libraries
* Copy agent.py from the cuckoo repository to the users startup folder
* Rename the extension from .py to .pyw
* Make sure no password is required to get to a desktop from boot
* Create a RunOnce key for c:\bootstrap.bat

When done, shutdown the virtual machine. Windows may choose to hibernate instead of shutting down, make sure the
guest has completely shut down. Remove the CD drive configuration from the virtual machine. The virtual machine will
fail if it contains any references to the install medium.

    sudo virsh edit Win7SP1x86

Create a snapshot of the virtual machine.

    sudo virsh snapshot-create Win7SP1x86

Verify that there is a "current" snapshot with the following command, it should result in a lot of XML.

    sudo virsh snapshot-current Win7SP1x86

#### Windows 10

Windows 10 is not *Officially* supported.

#### Android

Android is not *Officially* supported.

### Prepare the snapshot for Cuckoo

The prepare_vm command line will also differ depending on OS, and IP space. A sample for Windows 7 is provided 
below. 
    
    cd /opt/al/pkg/al_services/alsvc_cuckoo/vm
    sudo -u al PYTHONPATH=$PYTHONPATH ./prepare_vm.py --domain Win7SP1x86 --platform windows \
        --hostname PREPTEST --tags "pe32,default" --dns 192.168.100.10 --force --base Win7SP1x86 
        --name inetsim_Win7SP1x86  --guest_profile Win7SP1x86 --template win7 --ordinal 10 --route inetsim
    
The parameters for prepare_vm.py are:
* domain
  * The same as the virt-install --name argument
* platform
  * The "Cuckoo platform." Either "windows" or "linux" 
* ip, gateway, netmask, network, hostname, fakenet, dns
  * When running Cuckoo with multiple virtual machines, make sure that all prepared virtual machines have the same
gateway and network. They should have unique ip addresses. If you intend on using inetsim with a virtual machine,
the dns server and fakenet IP are the same. The fakenet IP should be the same for all virtual machines using inetsim,
they will share an inetsim docker container.
* tags
  * Comma separated list of tags which map to partial or full tags in common/constraints.py
  * Cuckoo will favour more specific tags
  * One VM may include the tag "default" to function as a default.
* force
  * Overwrite domain name if needed.
* base
  * Subdirectory of /opt/al/var/support/vm/disks/cuckoo/ containing the disk.
* name
  * Name of the new domain to create.
* guest_profile
  * The volatility profile
  * A list of all possible guest profiles is available on the [Volatility website.](https://github.com/volatilityfoundation/volatility/wiki/Volatility%20Usage#selecting-a-profile)
* template
  * The prepare_vm template, valid values are "win7", "win10", or "linux"

### Deploy all snapshots to Cuckoo

Once you've prepared all the virtual machine, there should be a number of .tar.gz files containing virtual machine
metadata. The prepare_cuckoo.py overwrites the current cuckoo configuration, so it's recommended to keep these files
handy in case you want to deploy new virtual machines in future. The prepare_cuckoo.py script will automatically
retrieve Cuckoo service configurations including metadata paths and enabled routes. If you change these configurations 
you will also need to run prepare_cuckoo.py again.

    cd /opt/al/pkg/al_services/alsvc_cuckoo/vm
    sudo -u al PYTHONPATH=$PYTHONPATH ./prepare_cuckoo.py *.tar.gz

## DEBUGGING

If you need to enter a running cuckoobox docker container while ASSEMBLYLINE is running, use the following command.

    sudo docker exec -ti `sudo docker ps | grep cuckoobox | cut -d ' ' -f 1` bash

To change the service configurations, use supervisorctl.

    supervisorctl -s unix:///tmp/supervisor.sock

You will find log files in /tmp and /opt/sandbox/bootstrap.log

If analysis sometimes succeeds and sometimes fails, make sure the tmpfs filesystem isn't filling up.

If you find that the Cuckoobox container exists immediately after being launched, this may be an out-of-memory issue on 
the ram mount inside the container. This directory is limited to 2 gigabytes by default, but can be modified in the 
ASSEMBLYLINE configurations. It must be large enough to store the snapshot image for all virtual machines with enough 
room left over for any given virtual machine to run a malware sample.