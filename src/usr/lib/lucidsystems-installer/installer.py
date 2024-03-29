import os
import subprocess
import time
import shutil
import gettext
import stat
import traceback
import commands
import sys
import parted

from subprocess import Popen
from configobj import ConfigObj
from PyQt4 import QtCore

class InstallerEngine(QtCore.QThread):
    ''' This is central to the lucidsystems installer '''

    def __init__(self, setup, parent = None):
        QtCore.QThread.__init__(self, parent)

        self.setup = setup
        self.conf_file = '/etc/lucidsystems-installer/install.conf'
        configuration = ConfigObj(self.conf_file)
        self.distribution_name = configuration['distribution']['DISTRIBUTION_NAME']
        self.installer_version = configuration['distribution']['INSTALLER_VERSION']   
        self.live_user = configuration['install']['LIVE_USER_NAME']
        self.root_image = configuration['install']['LIVE_MEDIA_ROOT_IMAGE']
        self.root_image_type = configuration['install']['LIVE_MEDIA_ROOT_IMAGE_TYPE']

    def __del__(self):
        self.wait()

    def run(self):
        self.install(self.setup)

    def update_progress(self, total, current, message):
        self.emit(QtCore.SIGNAL("progressUpdate(int, int, QString)"), total, current, QtCore.QString(message))

    def update_progressTextEdit(self, text):
        self.emit(QtCore.SIGNAL("progressUpdateTextEdit(QString)"), QtCore.QString(text))

    def error_message(self, message, critical=False):
        self.emit(QtCore.SIGNAL("errorMessage(QString, bool)"), message, critical)

    def get_distribution_name(self):
        return self.distribution_name

    def get_installer_version(self):
        return self.installer_version

    def step_format_partitions(self, setup):
        for partition in setup.partitions:                    
            if(partition.format_as is not None and partition.format_as != "" and partition.format_as != "None"):                
                # report it. should grab the total count of filesystems to be formatted ..
                self.update_progress(total=4, current=1, message="Formatting %(partition)s as %(format)s..." % {'partition':partition.partition.path, 'format':partition.format_as})
                
                if (partition.format_as == "fat16"):
                    partition.format_as = "msdos"
                elif (partition.format_as == "fat32"):
                    partition.format_as = "vfat"
                
                #Format it
                if partition.format_as == "swap":
                    cmd = "mkswap %s" % partition.partition.path
                else:
                    if (partition.format_as == "jfs"):
                        cmd = "mkfs.%s -q %s" % (partition.format_as, partition.partition.path)
                    elif (partition.format_as == "xfs"):
                        cmd = "mkfs.%s -f %s" % (partition.format_as, partition.partition.path)
                    elif (partition.format_as == "vfat"):
                        cmd = "mkfs.%s -F32 %s" % (partition.format_as, partition.partition.path)
                    else:
                        cmd = "mkfs.%s %s" % (partition.format_as, partition.partition.path) # works with bfs, btrfs, ext2, ext3, ext4, minix, msdos, ntfs
					
                print "EXECUTING: '%s'" % cmd
                p = Popen(cmd, shell=True)
                p.wait() # this blocks
                partition.type = partition.format_as
                                        
    def step_mount_partitions(self, setup):
        # Mount the installation media
        print " --> Mounting partitions"
        self.update_progress(total=3, current=2, message="Mounting %(partition)s on %(mountpoint)s" % {'partition':self.root_image, 'mountpoint':"/source/rootfs/"})
        print " ------ Mounting %s on %s" % (self.root_image, "/source/rootfs/")
        self.do_mount(self.root_image, "/source/rootfs/", self.root_image_type, options="loop")
        
        # Mount the target partition
        for partition in setup.partitions:
            if(partition.type == "fat16"):
                partition.type = "msdos"
            if(partition.type == "fat32"):
                partition.type = "vfat"                
            if(partition.mount_as is not None and partition.mount_as != ""):   
                  if partition.mount_as == "/":
                        self.update_progress(total=3, current=3, message="Mounting %(partition)s on %(mountpoint)s" % {'partition':partition.partition.path, 'mountpoint':"/target/"})
                        print " ------ Mounting %s on %s" % (partition.partition.path, "/target/")
                        self.do_mount(partition.partition.path, "/target", partition.type, None)
                        break
        
        # On efi systems we mount /boot before /boot/efi
        if(self.setup.bios_type == "efi"):
            for partition in setup.partitions:    
                if(partition.type == "fat16"):
                    partition.type = "msdos"
                if(partition.type == "fat32"):
                    partition.type = "vfat"
                if(partition.mount_as is not None and partition.mount_as != ""):   
                    if partition.mount_as == "/boot":
                            print " ------ Mounting %s on %s" % (partition.partition.path, "/target" + partition.mount_as)
                            self.do_run("mkdir -p /target" + partition.mount_as)
                            self.do_mount(partition.partition.path, "/target" + partition.mount_as, partition.type, None)
        
        # Mount the other partitions        
        for partition in setup.partitions:
            if(partition.type == "fat16"):
                partition.type = "msdos"
            if(partition.type == "fat32"):
                partition.type = "vfat"
            if(partition.mount_as is not None and partition.mount_as != "" and partition.mount_as != "/" and partition.mount_as != "swap"):
                print " ------ Mounting %s on %s" % (partition.partition.path, "/target" + partition.mount_as)
                self.do_run("mkdir -p /target" + partition.mount_as)
                # If the partition type is unknown, try auto
                if ((partition.type == "None") or (partition.type == "Unknown")):
                    partition.type = "auto"
                self.do_mount(partition.partition.path, "/target" + partition.mount_as, partition.type, None)

    def step_copy_files(self, source, destination):
        # walk filesystem
        directory_times = []
        our_total = 0
        our_current = -1
        os.chdir(source)
        # index the files
        print " --> Indexing files"
        for top,dirs,files in os.walk(source, topdown=False):
            our_total += len(dirs) + len(files)
            self.update_progress(total=0, current=0, message="Indexing files to be copied..")
        our_total += 1 # safenessness
        print " --> Copying files"
        for top,dirs,files in os.walk(source):
            # Sanity check. Python is a bit schitzo
            dirpath = top
            if(dirpath.startswith(source)):
                dirpath = dirpath[len(source):]
            for name in dirs + files:
                # following is hacked/copied from Ubiquity
                rpath = os.path.join(dirpath, name)
                sourcepath = os.path.join(source, rpath)
                targetpath = os.path.join(destination, rpath)
                st = os.lstat(sourcepath)
                mode = stat.S_IMODE(st.st_mode)

                # now show the world what we're doing                    
                our_current += 1
                self.update_progress(total=our_total, current=our_current, message="Copying %s" % rpath)

                if os.path.exists(targetpath):
                    if not os.path.isdir(targetpath):
                        os.remove(targetpath)                        
                if stat.S_ISLNK(st.st_mode):
                    if os.path.lexists(targetpath):
                        os.unlink(targetpath)
                    linkto = os.readlink(sourcepath)
                    os.symlink(linkto, targetpath)
                elif stat.S_ISDIR(st.st_mode):
                    if not os.path.isdir(targetpath):
                        os.mkdir(targetpath, mode)
                elif stat.S_ISCHR(st.st_mode):                        
                    os.mknod(targetpath, stat.S_IFCHR | mode, st.st_rdev)
                elif stat.S_ISBLK(st.st_mode):
                    os.mknod(targetpath, stat.S_IFBLK | mode, st.st_rdev)
                elif stat.S_ISFIFO(st.st_mode):
                    os.mknod(targetpath, stat.S_IFIFO | mode)
                elif stat.S_ISSOCK(st.st_mode):
                    os.mknod(targetpath, stat.S_IFSOCK | mode)
                elif stat.S_ISREG(st.st_mode):
                    # we don't do blacklisting yet..
                    try:
                        os.unlink(targetpath)
                    except:
                        pass
                    self.do_copy_file(sourcepath, targetpath)
                os.lchown(targetpath, st.st_uid, st.st_gid)
                if not stat.S_ISLNK(st.st_mode):
                    os.chmod(targetpath, mode)
                if stat.S_ISDIR(st.st_mode):
                    directory_times.append((targetpath, st.st_atime, st.st_mtime))
                # os.utime() sets timestamp of target, not link
                elif not stat.S_ISLNK(st.st_mode):
                    os.utime(targetpath, (st.st_atime, st.st_mtime))
        # Apply timestamps to all directories now that the items within them
        # have been copied.
        print " --> Restoring meta-info"
        for dirtime in directory_times:
            (directory, atime, mtime) = dirtime
            try:
                self.update_progress(total=0, current=0, message="Restoring meta-information on %s" % directory)
                os.utime(directory, (atime, mtime))
            except OSError:
                pass
        

    def install(self, setup):
        # mount the media location.
        print " --> Installation started"
        try:
            # create target dir
            if(not os.path.exists("/target")):
                os.mkdir("/target")
            
            # create source dirs
            if(not os.path.exists("/source")):
                os.mkdir("/source")
            if(not os.path.exists("/source/rootfs")):
                os.mkdir("/source/rootfs")

            # find the source images..
            if(not os.path.exists(self.root_image)):
                print "The base filesystem does not exist! Critical error (exiting)."
                self.error_message(message="The source image doesn't exist! Aborting!", critical=True)
                self.exit(1)

            # format partitions
            self.step_format_partitions(setup)
            
            # mount all needed partitions
            self.step_mount_partitions(setup)
            
            # copy root image                    
            self.step_copy_files(source="/source/rootfs/", destination="/target/")

            # Steps:
            our_total = 14
            our_current = 0
            # chroot
            print " --> Chrooting"
            self.update_progress(total=our_total, current=our_current, message="Entering new system..")

            # setup mountpoints
            if(not os.path.exists("/target/proc")):
                os.mkdir("/target/proc")
            self.do_run("mount -t proc proc /target/proc/")

            if(not os.path.exists("/target/sys")):
                os.mkdir("/target/sys")
            self.do_run("mount -t sysfs sys /target/sys/")

            if(not os.path.exists("/target/dev")):
                os.mkdir("/target/dev")
            self.do_run("mount --bind /dev/ /target/dev/")

            # shared memory
            if(not os.path.exists("/target/dev/shm")):
                os.mkdir("/target/dev/shm")
            self.do_run("mount --bind /dev/shm/ /target/dev/shm/")

            # important for pacman (for signature check)
            if(not os.path.exists("/target/dev/pts")):
                os.mkdir("/target/dev/pts")
            self.do_run("mount -t devpts pts /target/dev/pts/")

            # this is needed to use networking within the chroot
            self.do_run("cp -f /etc/resolv.conf /target/etc/resolv.conf")

            # initialize pacman keyring
            print " --> Initializing pacman keyring"
            self.update_progress(total=0, current=0, message="Configuring Pacman")
            our_current += 1
            self.do_run_in_chroot("rm -rf /etc/pacman.d/gnupg")
            self.do_run_in_chroot("pacman-key --init")
            self.do_run_in_chroot("pacman-key --populate archlinux")
            self.do_run_in_chroot("pacman-key --populate lucidsystems")
            self.do_run_in_chroot("pacman-key --refresh-keys")

            # optimize mirrorlist
            our_current += 1
            if (setup.internet_connectivity == True):
                print " --> Optimizing pacman mirrorlist"
                self.update_progress(total=our_total, current=our_current, message="Optimizing pacman mirrorlist")
                self.do_run_in_chroot("pacman -S --noconfirm reflector")
                self.do_run_in_chroot("reflector -l 8 --sort rate --save /etc/pacman.d/mirrorlist")
            else:
                self.update_progress(total=our_total, current=our_current, message="Skip optimizing pacman mirrorlist")

            # remove live configuration packages
            print " --> Removing livesystem configuration"
            our_current += 1
            self.update_progress(total=our_total, current=our_current, message="Removing livesystem configuration")
            self.do_run_in_chroot("pacman -R --noconfirm lucidsystems-installer")
            self.do_run_in_chroot("pacman -R --noconfirm lucidsystems-livemedia")
            
            if(os.path.exists("/target/etc/skel/Desktop/lucidsystems-Installer.desktop")):
                self.do_run_in_chroot("rm -f /etc/skel/Desktop/lucidsystems-Installer.desktop")

            if(os.path.exists("/target/usr/share/applications/lucidsystems-installer-launcher.desktop")):
                self.do_run_in_chroot("rm -f /usr/share/applications/lucidsystems-installer-launcher.desktop")

            if(os.path.exists("/target/etc/skel/.config/autostart/lucidsystems-greeter.desktop")):
                self.do_run_in_chroot("rm -f /etc/skel/.config/autostart/lucidsystems-greeter.desktop")

            # remove liveuser service
            if(os.path.exists("/target/usr/bin/prepare_livesystem")):
                self.do_run_in_chroot("rm -rf /usr/bin/prepare_livesystem")
            if(os.path.exists("/target/etc/systemd/system/prepare_livesystem.service")):
                self.do_run_in_chroot("rm -rf /etc/systemd/system/prepare_livesystem.service")
            if(os.path.exists("/target/etc/systemd/system/multi-user.target.wants/prepare_livesystem.service")):
                self.do_run_in_chroot("rm -rf /etc/systemd/system/multi-user.target.wants/prepare_livesystem.service")

            # add new user
            print " --> Adding new user"
            our_current += 1
            self.update_progress(total=our_total, current=our_current, message="Creating new user")         
            self.do_run_in_chroot("useradd -s %s -c \'%s\' -G audio,games,lp,nopasswdlogin,optical,power,scanner,shutdown,storage,sudo,video -m %s" % ("/bin/bash", setup.real_name, setup.username))
            newusers = open("/target/tmp/newusers.conf", "w")
            newusers.write("%s:%s\n" % (setup.username, setup.password1))
            newusers.write("root:%s\n" % setup.password1)
            newusers.close()
            self.do_run_in_chroot("cat /tmp/newusers.conf | chpasswd")
            self.do_run_in_chroot("rm -rf /tmp/newusers.conf")
            
            # write the /etc/fstab
            print " --> Writing fstab"
            our_current += 1
            self.update_progress(total=our_total, current=our_current, message="Writing filesystem mount information")
            # make sure fstab has default /proc and /sys entries
            if(not os.path.exists("/target/etc/fstab")):
                self.do_run("echo \"#### Static Filesystem Table File\" > /target/etc/fstab")
            fstab = open("/target/etc/fstab", "a")
            fstab.write("proc\t/proc\tproc\tdefaults\t0\t0\n")
            for partition in setup.partitions:
                if (partition.mount_as is not None and partition.mount_as != "None"):
                    partition_uuid = partition.partition.path # If we can't find the UUID we use the path
                    try:                    
                        blkid = commands.getoutput('blkid').split('\n')
                        for blkid_line in blkid:
                            blkid_elements = blkid_line.split(':')
                            if blkid_elements[0] == partition.partition.path:
                                blkid_mini_elements = blkid_line.split()
                                for blkid_mini_element in blkid_mini_elements:
                                    if "UUID=" in blkid_mini_element:
                                        partition_uuid = blkid_mini_element.replace('"', '').strip()
                                        break
                                break
                    except Exception:
                        print '-'*60
                        traceback.print_exc(file=sys.stdout)
                        print '-'*60
                                        
                    fstab.write("# %s\n" % (partition.partition.path))                            
                    
                    if(partition.mount_as == "/"):
                        fstab_fsck_option = "1"
                    else:
                        fstab_fsck_option = "0" 
                                            
                    if("ext" in partition.type):
                        fstab_mount_options = "rw,errors=remount-ro"
                    else:
                        fstab_mount_options = "defaults"
                        
                    if(partition.type == "swap"):                    
                        fstab.write("%s\tswap\tswap\tsw\t0\t0\n" % partition_uuid)
                    else:                                                    
                        fstab.write("%s\t%s\t%s\t%s\t%s\t%s\n" % (partition_uuid, partition.mount_as, partition.type, fstab_mount_options, "0", fstab_fsck_option))
            fstab.close()
            
            # write host+hostname infos
            print " --> Writing hostname"
            our_current += 1
            self.update_progress(total=our_total, current=our_current, message="Setting hostname")
            hostnamefh = open("/target/etc/hostname", "w")
            hostnamefh.write("%s\n" % setup.hostname)
            hostnamefh.close()
            hostsfh = open("/target/etc/hosts", "w")
            hostsfh.write("127.0.0.1\tlocalhost\n")
            hostsfh.write("127.0.1.1\t%s\n" % setup.hostname)
            hostsfh.write("# The following lines are desirable for IPv6 capable hosts\n")
            hostsfh.write("::1     localhost ip6-localhost ip6-loopback\n")
            hostsfh.write("fe00::0 ip6-localnet\n")
            hostsfh.write("ff00::0 ip6-mcastprefix\n")
            hostsfh.write("ff02::1 ip6-allnodes\n")
            hostsfh.write("ff02::2 ip6-allrouters\n")
            hostsfh.write("ff02::3 ip6-allhosts\n")
            hostsfh.close()

            # set the locale
            print " --> Setting the locale"
            our_current += 1
            self.update_progress(total=our_total, current=our_current, message="Setting locale")
            self.do_run("echo \"%s.UTF-8 UTF-8\" >> /target/etc/locale.gen" % setup.locale_code)
            self.do_run_in_chroot("locale-gen")
            self.do_run("echo \"\" > /target/etc/default/locale")
            self.do_run_in_chroot("echo \"LANG=%s.UTF-8\" > /etc/locale.conf" % setup.locale_code)
            self.do_run_in_chroot("echo \"LC_TIME=%s.UTF-8\" >> /etc/locale.conf" % setup.locale_code)

            # set the timezone
            print " --> Setting the timezone"
            our_current += 1
            self.update_progress(total=our_total, current=our_current, message="Setting timezone")
            self.do_run("echo \"%s\" > /target/etc/timezone" % setup.timezone_code)
            self.do_run_in_chroot("rm -f /etc/localtime")
            self.do_run_in_chroot("ln -s /usr/share/zoneinfo/%s /etc/localtime" % setup.timezone)

            # set the keyboard options..
            print " --> Configuring keyboard"
            our_current += 1
            self.update_progress(total=our_total, current=our_current, message="Configuring Keyboard")
            self.do_run_in_chroot("echo \"KEYMAP=%s\" > /etc/vconsole.conf" % setup.keyboard_layout)
            self.do_run_in_chroot("echo \"FONT=\" >> /etc/vconsole.conf")
            self.do_run_in_chroot("echo \"FONT_MAP=\" >> /etc/vconsole.conf")
            
            # create xorg config for keyboard
            keyboardfh = open("/target/etc/X11/xorg.conf.d/90-keyboard-layouts.conf", "w")
            keyboardfh.write("Section \"InputClass\"\n")
            keyboardfh.write("  Identifier      \"MainKeyboard\"\n")
            keyboardfh.write("  MatchIsKeyboard \"on\"\n")
            keyboardfh.write("  MatchDevicePath \"/dev/input/event*\"\n")
            keyboardfh.write("  Driver          \"evdev\"\n")
            keyboardfh.write("  Option          \"XkbModel\"      \"%s\"\n" % setup.keyboard_model)
            keyboardfh.write("  Option          \"XkbLayout\"     \"%s\"\n" % setup.keyboard_layout)
            keyboardfh.write("  Option          \"XkbVariant\"    \"%s\"\n" % setup.keyboard_variant)
            keyboardfh.write("  Option          \"XkbOptions\"    \"\"\n")
            keyboardfh.write("EndSection\n")
            keyboardfh.close()

            # configure lightdm
            print " --> Configuring LightDM"
            self.update_progress(total=our_total, current=our_current, message="Configuring LightDM")
            our_current += 1
            lightdmconfig = open("/target/etc/lightdm/lightdm.conf", "r")
            newlightdmconfig = open("/target/etc/lightdm/lightdm.conf.new", "w")
            for line in lightdmconfig:
                line = line.rstrip("\r\n")
                if(line.startswith("greeter-session=")):
                    newlightdmconfig.write("greeter-session=lightdm-gtk-greeter\n")
                elif(line.startswith("#greeter-session=")):
                    newlightdmconfig.write("greeter-session=lightdm-gtk-greeter\n")
                else:
                    newlightdmconfig.write("%s\n" % line)
            lightdmconfig.close()
            newlightdmconfig.close()
            self.do_run_in_chroot("rm /etc/lightdm/lightdm.conf")
            self.do_run_in_chroot("mv /etc/lightdm/lightdm.conf.new /etc/lightdm/lightdm.conf")

            # generate ramdisk
            print " --> Generating Ramdisk"
            self.update_progress(total=0, current=0, message="Generating ramdisk")
            our_current += 1
            self.do_run_in_chroot("mkinitcpio -p linux")

            # install grub
            print " --> Configuring Grub"
            our_current += 1
            if(setup.bootloader_device is not None):
                self.update_progress(total=our_total, current=our_current, message="Installing bootloader")

                if(self.setup.bios_type == "efi"):
                    # EFI
                    print " --> Installing grub (efi)"
                    self.do_run_in_chroot("pacman -S --noconfirm --force %s" % setup.bootloader_type)
                    if(not os.path.exists("/target%s" % setup.bootloader_device)):
                        os.mkdir("/target%s" % setup.bootloader_device)
                    self.do_run_in_chroot("grub-install --target=x86_64-efi --efi-directory=%s --bootloader-id=lucidsystems --force"  % setup.bootloader_device)
                else:
                    # BIOS
                    print " --> Installing grub (bios)"
                    self.do_run_in_chroot("pacman -S --noconfirm --force %s" % setup.bootloader_type)
                    self.do_run_in_chroot("grub-install --target=i386-pc --force %s" % setup.bootloader_device)

                if(not os.path.exists("/target/boot/grub/locale")):
                    os.mkdir("/target/boot/grub/locale")
                self.do_run_in_chroot("cp /usr/share/locale/en\@quot/LC_MESSAGES/grub.mo /boot/grub/locale/en.mo")

                self.do_configure_grub(our_total, our_current)
                grub_retries = 0
                while (not self.do_check_grub(our_total, our_current)):
                    self.do_configure_grub(our_total, our_current)
                    grub_retries = grub_retries + 1
                    if grub_retries >= 5:
                        self.error_message(message="The bootloader wasn't configured properly! You need to configure it manually.", critical=True)
                        self.exit(2)
                        break

            # install user selected packages
            our_current += 1
            if (setup.internet_connectivity == True):
                if setup.installList:
                    print " --> Installing additional packages"
                    for package in setup.installList:
                        self.update_progress(total=our_total, current=our_current, message="Installing %s" % package)
                        self.do_run_in_chroot("pacman -S --noconfirm %s" % package)

            # now unmount it
            our_current += 1
            print " --> Unmounting partitions"
            try:
                self.do_run("umount --force /target/dev/shm")
                self.do_run("umount --force /target/dev/pts")
                self.do_run("umount --force /target/dev/")
                self.do_run("umount --force /target/sys/")
                self.do_run("umount --force /target/proc/")
                self.do_run("rm -rf /target/etc/resolv.conf")
                for partition in setup.partitions:
                    if(partition.mount_as is not None and partition.mount_as != "" and partition.mount_as != "/" and partition.mount_as != "swap"):
                        self.do_unmount("/target" + partition.mount_as)
                self.do_unmount("/target")
                self.do_unmount("/source/rootfs")
            except Exception:
                print '-'*60
                traceback.print_exc(file=sys.stdout)
                print '-'*60

            self.update_progress(total=100, current=100, message="Installation finished")
            print " --> All done"

            self.emit(QtCore.SIGNAL("installFinished()"))
            self.exit(0)
            
        except Exception:            
            print '-'*60
            traceback.print_exc(file=sys.stdout)
            print '-'*60

    def do_run(self, command):
        os.system(command)
        output = commands.getoutput(command)
        self.update_progressTextEdit(output)

    def do_run_in_chroot(self, command):
        os.system("chroot /target/ /usr/bin/sh -c \"%s\"" % command)
        output = commands.getoutput("chroot /target/ /usr/bin/sh -c \"%s\"" % command)
        self.update_progressTextEdit(output)
        
    def do_configure_grub(self, our_total, our_current):
        self.update_progress(total=0, current=0, message="Configuring bootloader")

        # Workaround for https://bugs.archlinux.org/task/37904
        self.do_run_in_chroot("rm -f /etc/grub.d/10_linux")

        print " --> Running grub-mkconfig"
        self.do_run_in_chroot("grub-mkconfig -o /boot/grub/grub.cfg")
        grub_output = commands.getoutput("chroot /target/ /usr/bin/sh -c \"grub-mkconfig -o /boot/grub/grub.cfg\"")
        grubfh = open("/var/log/live-installer-grub-output.log", "w")
        grubfh.writelines(grub_output)
        grubfh.close()
        
    def do_check_grub(self, our_total, our_current):
        self.update_progress(total=0, current=0, message="Checking bootloader")
        print " --> Checking Grub configuration"
        time.sleep(5)
        found_theme = False
        found_entry = False
        if os.path.exists("/target/boot/grub/grub.cfg"):
            grubfh = open("/target/boot/grub/grub.cfg", "r")
            for line in grubfh:
                line = line.rstrip("\r\n")
                if("lucidsystems.png" in line):
                    found_theme = True
                    print " --> Found Grub theme: %s " % line
                if ("menuentry" in line and "Arch Linux" in line):
                    found_entry = True
                    print " --> Found Grub entry: %s " % line
            grubfh.close()
            return (found_entry)
        else:
            print "!No /target/boot/grub/grub.cfg file found!"
            return False

    def do_mount(self, device, dest, type, options=None):
        ''' Mount a filesystem '''
        p = None
        if(options is not None):
            cmd = "mount -o %s -t %s %s %s" % (options, type, device, dest)            
        else:
            cmd = "mount -t %s %s %s" % (type, device, dest)
        print "EXECUTING: '%s'" % cmd
        p = Popen(cmd ,shell=True)        
        p.wait()
        return p.returncode

    def do_unmount(self, mountpoint):
        ''' Unmount a filesystem '''
        cmd = "umount %s" % mountpoint
        print "EXECUTING: '%s'" % cmd
        p = Popen(cmd, shell=True)
        p.wait()
        return p.returncode

    def do_copy_file(self, source, dest):
        # TODO: Add md5 checks. BADLY needed..
        BUF_SIZE = 16 * 1024
        input = open(source, "rb")
        dst = open(dest, "wb")
        while(True):
            read = input.read(BUF_SIZE)
            if not read:
                break
            dst.write(read)
        input.close()
        dst.close()

class Setup(object):
    locale_code = None
    country_code = None
    timezone = None
    timezone_code = None
    keyboard_model = None    
    keyboard_layout = None    
    keyboard_variant = None    
    partitions = [] # Array of PartitionSetup objects
    username = None
    hostname = None
    password1 = None
    password2 = None
    real_name = None
    bios_type = "bios"
    bootloader_type = None
    bootloader_device = None
    disks = []
    target_disk = None
    internet_connectivity = False
    installList = []
    
    # Descriptions (used by the summary screen)    
    keyboard_model_description = None
    keyboard_layout_description = None
    keyboard_variant_description = None
    
    def print_setup(self):
        if "--debug" in sys.argv:  
            print "-------------------------------------------------------------------------"
            print "locale: %s" % self.locale_code
            print "country: %s" % self.country_code
            print "timezone: %s (%s)" % (self.timezone, self.timezone_code)        
            print "keyboard: %s - %s (%s) - %s - %s (%s)" % (self.keyboard_model, self.keyboard_layout, self.keyboard_variant, self.keyboard_model_description, self.keyboard_layout_description, self.keyboard_variant_description)        
            print "user: %s (%s)" % (self.username, self.real_name)
            print "hostname: %s " % self.hostname
            print "passwords: %s - %s" % (self.password1, self.password2)
            print "bios_type: %s" % self.bios_type
            print "bootloader_type: %s " % self.bootloader_type
            print "bootloader_device: %s " % self.bootloader_device
            print "target_disk: %s " % self.target_disk
            print "disks: %s " % self.disks                       
            print "partitions:"
            for partition in self.partitions:
                partition.print_partition()
            print "-------------------------------------------------------------------------"
            print "Internet connectivity: %s" % self.internet_connectivity
            print "-------------------------------------------------------------------------"
            print "Additional packages:"
            for package in setup.installList:
                print package
            print "-------------------------------------------------------------------------"

class PartitionSetup(object):
    name = ""    
    type = ""
    format_as = None
    mount_as = None    
    partition = None
    aggregatedPartitions = []

    def __init__(self, partition):
        self.partition = partition
        self.size = partition.getSize()
        self.start = partition.geometry.start
        self.end = partition.geometry.end
        self.description = ""
        self.used_space = ""
        self.free_space = ""

        if partition.number != -1:
            self.name = partition.path            
            if partition.fileSystem is None:
                # no filesystem, check flags
                if partition.type == parted.PARTITION_SWAP:
                    self.type = ("Linux swap")
                elif partition.type == parted.PARTITION_RAID:
                    self.type = ("RAID")
                elif partition.type == parted.PARTITION_LVM:
                    self.type = ("Linux LVM")
                elif partition.type == parted.PARTITION_HPSERVICE:
                    self.type = ("HP Service")
                elif partition.type == parted.PARTITION_PALO:
                    self.type = ("PALO")
                elif partition.type == parted.PARTITION_PREP:
                    self.type = ("PReP")
                elif partition.type == parted.PARTITION_MSFT_RESERVED:
                    self.type = ("MSFT Reserved")
                elif partition.type == parted.PARTITION_EXTENDED:
                    self.type = ("Extended Partition")
                elif partition.type == parted.PARTITION_LOGICAL:
                    self.type = ("Logical Partition")
                elif partition.type == parted.PARTITION_FREESPACE:
                    self.type = ("Free Space")
                else:
                    self.type =("Unknown")
            else:
                self.type = partition.fileSystem.type
        else:
            self.type = ""
            self.name = "unallocated"

    def add_partition(self, partition):
        self.aggregatedPartitions.append(partition)
        self.size = self.size + partition.getSize()
        self.end = partition.geometry.end
    
    def print_partition(self):
        print "Device: %s, format as: %s, mount as: %s" % (self.partition.path, self.format_as, self.mount_as)
