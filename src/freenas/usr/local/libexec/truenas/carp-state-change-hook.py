#!/usr/bin/env python
# Copyright (c) 2015 iXsystems, Inc.
# All rights reserved.
# This file is a part of TrueNAS
# and may not be copied and/or distributed
# without the express permission of iXsystems.

from lockfile import LockFile, AlreadyLocked

import atexit
import json
import logging
import logging.config
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import time


# TODO /tmp/failover/

# GUI sentinel files
ELECTING_FILE = '/tmp/.failover_electing'
IMPORTING_FILE = '/tmp/.failover_importing'
FAILED_FILE = '/tmp/.failover_failed'
FAILOVER_ASSUMED_MASTER = '/tmp/.failover_master'

# Config file, externally generated
FAILOVER_JSON = '/tmp/failover.json'

# MUTEX files
# Advanced TODO Merge to one mutex
FAILOVER_IFQ = '/tmp/.failover_ifq'
FAILOVER_EVENT = '/tmp/.failover_event'

# Fast track, user initiated failover
FAILOVER_OVERRIDE = '/tmp/failover_override'

# GUI sentinel file
FAILOVER_STATE = '/tmp/.failover_state'

# This sentinel is created by the pool decryption
# script to let us know we need to do something
FAILOVER_NEEDOP = '/tmp/.failover_needop'

# These files are created by a cron job
# and used to fast track determining a master
HEARTBEAT_BARRIER = '/tmp/heartbeat_barrier'
HEARTBEAT_STATE = '/tmp/heartbeat_state'

# FAILOVER_IFQ is the mutex used to protect per-interface events.
# Before creating the lockfile that this script is handing events
# on a given interface, this lock is aquired.  This lock attempt
# sleeps indefinitely, which is (ab)used to create an event queue.
# For instance, if igb0 link_down, link_up, link_down happens in
# rapid succession, the script will fire with all three events, but
# event two and three will wait until event one runs to completion.
# It's important to note that when the event handlers fire one of
# the first things they do is check to see if the event that fired
# them is still in affect.  For instance, by the time the link_up
# event handler runs in this example igb0 will be link_down and it
# will exit.

# FAILOVER_EVENT is the mutex to protect the critical sections of
# the "become active" or "become standby" actions in this script.
# This is needed in situations where there are multiple interfaces
# that all go link_up or link_down simultaniously (such as when the
# partner node reboots).  In that case each interface will acquire
# it's per-interface lock and run the link_up or link_down event
# FAILOVER_EVENT prevents them both from starting fenced or
# importing volumes or whatnot.

logging.raiseExceptions = False  # Please, don't hate us this much
log = logging.getLogger('carp-state-change-hook')


def run(cmd):
    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        shell=True,
    )
    output = proc.communicate()[0]
    return (proc.returncode, output.strip('\n'))


def run_async(cmd):
    subprocess.Popen(
            cmd,
            shell=True,
            )
    return


def main(subsystem, event):

    if '@' not in subsystem:
        sys.exit(1)
    vhid, ifname = subsystem.split('@', 1)

    if event == 'forcetakeover':
        forcetakeover = True
    else:
        forcetakeover = False

    if not os.path.exists(FAILOVER_JSON):
        log.warn("No %s found, exiting.", FAILOVER_JSON)
        sys.exit(1)

    # TODO write the PID into the state file so a stale
    # lockfile won't disable HA forever
    state_file = '%s%s' % (FAILOVER_STATE, event)

    @atexit.register
    def cleanup():
        try:
            os.unlink(state_file)
        except:
            pass

    # Implicit event queuing
    with LockFile(FAILOVER_IFQ):
        if not os.path.exists(state_file):
            open(state_file, 'w').close()
        else:
            sys.exit(0)

    with open(FAILOVER_JSON, 'r') as f:
        fobj = json.loads(f.read())

    # The failover sript doesn't handle events on the
    # internal interlink
    if ifname in fobj['internal_interfaces']:
        # TODO log these events
        sys.exit(1)

    # TODO python any
    if not forcetakeover:
        SENTINEL = False
        for group in fobj['groups']:
            for interface in fobj['groups'][group]:
                if ifname == interface:
                    SENTINEL = True

        if not SENTINEL:
            log.warn("Ignoring state change on non-critical interface %s.", ifname)
            sys.exit()

        if fobj['disabled']:
            if not fobj['master']:
                log.warn("Failover disabled.  Assuming backup.")
                sys.exit()
            else:

                # We need to load django for notifier
                sys.path.append('/usr/local/www')
                sys.path.append('/usr/local/www/freenasUI')
                sys.path.append('/usr/local/lib')

                os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freenasUI.settings')

                # Make sure to load all modules
                from django.db.models.loading import cache
                cache.get_apps()

                from freenasUI.middleware.notifier import notifier

                try:
                    s = notifier().failover_rpc()
                    status = s.notifier("failover_status", None, None)
                    if status == 'MASTER':
                        log.warn("Other node is already active, assuming backup.")
                        sys.exit()
                except Exception as e:
                    log.info("Failed to contact the other node", exc_info=True)
                    print e, "Failed to contact the other node"

                masterret = False
                for vol in fobj['volumes'] + fobj['phrasedvolumes']:
                    # TODO run, or zfs lib
                    ret = os.system("zpool status %s > /dev/null" % vol)
                    if ret:
                        masterret = True
                        for group in fobj['groups']:
                            for interface in fobj['groups'][group]:
                                error, output = run("ifconfig %s | grep 'carp:' | awk '{print $4}'" % interface)
                                for vhid in output.split():
                                    run("ifconfig %s vhid %s advskew 0" % (interface, vhid))
                        log.warn("Failover disabled.  Assuming active.")
                        run("touch %s" % FAILOVER_OVERRIDE)
                if masterret is False:
                    # All pools are already imported
                    sys.exit()

    open(HEARTBEAT_BARRIER, 'a+').close()

    now = int(time.time())
    os.utime(HEARTBEAT_BARRIER, (now, now))

    user_override = True if os.path.exists(FAILOVER_OVERRIDE) else False

    if event == 'MASTER' or event == 'forcetakeover':
        carp_master(fobj, state_file, ifname, vhid, event, user_override, forcetakeover)
    elif event == 'BACKUP' or event == 'INIT':
        carp_backup(fobj, state_file, ifname, vhid, event, user_override)


def carp_master(fobj, state_file, ifname, vhid, event, user_override, forcetakeover):

    if forcetakeover:
        log.warn("Starting force takeover.")
    else:
        log.warn("Entering MASTER on %s", ifname)

    if not user_override and not forcetakeover:
        sleeper = fobj['timeout']
        # The specs for lagg require that if a subinterface of the lagg interface
        # changes state, all traffic on the entire logical interface will be halted
        # for two seconds while the bundle reconverges.  This means if there's a
        # toplogy change on the active node, the standby node will get a link_up
        # event on the lagg.  To  prevent the standby node from immediately pre-empting
        # we wait 2 seconds to see if the evbent was transient.
        if ifname.startswith("lagg"):
            if sleeper < 2:
                sleeper = 2
            log.warn("Sleeping %s seconds and rechecking %s", sleeper, ifname)
            time.sleep(sleeper)
            error, output = run(
                "ifconfig %s | grep 'carp:' | grep 'vhid %s ' | awk '{print $2}'" % (ifname, vhid)
            )
            if output != 'MASTER':
                log.warn("%s became %s. Previous event ignored.", ifname, output)
                sys.exit(0)
        else:
            if sleeper != 0:
                log.warn("Sleeping %s seconds and rechecking %s", sleeper, ifname)
                time.sleep(sleeper)
                error, output = run(
                    "ifconfig %s | grep 'carp:' | grep 'vhid %s ' | awk '{print $2}'" % (ifname, vhid)
                )
                if output != 'MASTER':
                    log.warn("%s became %s. Previous event ignored.", ifname, output)
                    sys.exit(0)

    if os.path.exists(FAILOVER_ASSUMED_MASTER) or forcetakeover:
        error, output = run("ifconfig -l")
        for iface in list(output.split()):
            if iface in fobj['internal_interfaces']:
                continue
            error, output = run("ifconfig %s | grep 'carp:' | awk '{print $4}'" % iface)
            for vhid in list(output.split()):
                run("ifconfig %s vhid %s advskew 1" % (iface, vhid))
        if not forcetakeover:
            sys.exit(0)

    if not forcetakeover:
        totoutput = 0
        for group, carpint in fobj['groups'].items():
            for i in carpint:
                error, output = run("ifconfig %s | grep 'carp: BACKUP' | wc -l" % i)
                totoutput += int(output)

                if not error and totoutput > 0:
                    log.warn(
                        'Ignoring UP state on %s because we still have interfaces that are'
                        ' BACKUP.', ifname
                    )
                    run_async('echo "$(date), $(hostname), %s assumed master while other '
                              'interfaces are still in slave mode." | mail -s "Failover WARNING"'
                              ' root' % ifname)
                    sys.exit(1)

    if not forcetakeover:
        run('pkill -9 -f fenced')

    try:
        os.unlink(FAILED_FILE)
    except:
        pass
    try:
        os.unlink(IMPORTING_FILE)
    except:
        pass
    open(ELECTING_FILE, 'w').close()

    fasttrack = False
    if not forcetakeover:
        was_connected = True if (
            os.path.exists(HEARTBEAT_STATE) and
            os.stat(HEARTBEAT_STATE).st_mtime > os.stat(HEARTBEAT_BARRIER).st_mtime
        ) else False

        if was_connected:
            time.sleep(1)
            error, status0 = run(
                "ifconfig %s | grep 'carp:' | awk '{print $2}'" % ifname
            )
            if fobj['internal_interfaces']:
                intiface = fobj['internal_interfaces'][0]
            else:
                intiface = ''
            error, status1 = run(
                "ifconfig %s | grep carp: | grep -E 'vhid (10|20) ' | awk '{print $2;}' "
                "|grep -E '(MASTER|INIT)' | wc -l" % intiface
            )
            error, status2 = run(
                "ifconfig %s | grep carp: | grep -E 'vhid (10|20) ' | awk '{print $2;}' "
                "|grep BACKUP | wc -l" % intiface
            )

            log.warn('Status: %s:%s:%s', status0, status1, status2)

            if status0 != 'MASTER':
                log.warn('Promoted then demoted, quitting.')
                # Just in case.  Demote ourselves.
                run('ifconfig %s vhid %s advskew 206' % (ifname, vhid))
                try:
                    os.unlink(ELECTING_FILE)
                except:
                    pass
                sys.exit(0)

            if int(status1) == 2 and int(status2) == 0:
                fasttrack = True

    # Start the critical section
    try:
        with LockFile(FAILOVER_EVENT, timeout=0):
            # The lockfile modules cleans up lockfiles if this script exits on it's own accord.
            # For reboots, /tmp is cleared by virtue of being a memory device.
            # If someone does a kill -9 on the script while it's running the lockfile
            # will get left dangling.
            log.warn('Aquired failover master lock')
            log.warn('Starting fenced')
            run('/sbin/camcontrol rescan all')
            if not user_override and not fasttrack and not forcetakeover:
                error, output = run('LD_LIBRARY_PATH=/usr/local/lib /usr/local/bin/python /usr/local/sbin/fenced')
            else:
                error, output = run(
                    'LD_LIBRARY_PATH=/usr/local/lib /usr/local/bin/python /usr/local/sbin/fenced force'
                )

            if error:
                if error == 1:
                    log.warn('Can not register keys on disks!')
                    run('ifconfig %s vhid %s advskew 201' % (ifname, vhid))
                elif error == 2:
                    log.warn('Remote fenced is running!')
                    run('ifconfig %s vhid %s advskew 202' % (ifname, vhid))
                elif error == 3:
                    log.warn('Can not reserve all disks!')
                    run('ifconfig %s vhid %s advskew 203' % (ifname, vhid))
                elif error == 5:
                    log.warn('Fencing daemon encountered an unexpected fatal error!')
                    run('ifconfig %s vhid %s advskew 205' % (ifname, vhid))
                else:
                    log.warn('This should never happen: %d', error)
                    run('ifconfig %s vhid %s advskew 204' % (ifname, vhid))
                try:
                    os.unlink(ELECTING_FILE)
                except:
                    pass
                sys.exit(1)

            # If we reached here, fenced is daemonized and have all drives reserved.
            # Bring up all carps we own.
            error, output = run("ifconfig -l")
            for iface in output.split():
                for iface in list(output.split()):
                    if iface in fobj['internal_interfaces']:
                        continue
                    error, output = run("ifconfig %s | grep 'carp:' | awk '{print $4}'" % iface)
                    for vhid in list(output.split()):
                        run("ifconfig %s vhid %s advskew 1" % (iface, vhid))

            open(IMPORTING_FILE, 'w').close()
            try:
                os.unlink(ELECTING_FILE)
            except:
                pass

            run("sysctl -n kern.disks | tr ' ' '\\n' | sed -e 's,^,/dev/,' | grep '^/dev/da' | xargs -n 1 echo 'false >' | sh")

            if os.path.exists('/data/zfs/killcache'):
                run('rm -f /data/zfs/zpool.cache /data/zfs/zpool.cache.saved')
            else:
                open('/data/zfs/killcache', 'w').close()
                run('fsync /data/zfs/killcache')

            if os.path.exists('/data/zfs/zpool.cache'):
                stat1 = os.stat('/data/zfs/zpool.cache')
                if (
                    not os.path.exists('/data/zfs/zpool.cache.saved') or
                    stat1.st_mtime > os.stat('/data/zfs/zpool.cache.saved').st_mtime
                ):
                    run('cp /data/zfs/zpool.cache /data/zfs/zpool.cache.saved')

            log.warn('Beginning volume imports.')
            # TODO: now that we are all python, we should probably just absorb the code in.
            run(
                'LD_LIBRARY_PATH=/usr/local/lib /usr/local/sbin/enc_helper attachall'
            )

            p = multiprocessing.Process(target=os.system("""dtrace -qn 'zfs-dbgmsg{printf("\r                            \r%s", stringof(arg0))}' > /dev/console &"""))
            p.start()
            for volume in fobj['volumes']:
                log.warn('Importing %s', volume)
                error, output = run('/sbin/zpool import %s -o cachefile=none -m -R /mnt -f %s' % (
                    '-c /data/zfs/zpool.cache.saved' if os.path.exists(
                        '-c /data/zfs/zpool.cache.saved'
                    ) else '',
                    volume,
                ))
                if error:
                    open(FAILED_FILE, 'w').close()
                run('/sbin/zpool set cachefile=/data/zfs/zpool.cache %s' % volume)

            p.terminate()
            os.system("pkill -9 -f 'dtrace -qn'")
            if not os.path.exists(FAILOVER_NEEDOP):
                open(FAILOVER_ASSUMED_MASTER, 'w').close()

            try:
                os.unlink('/data/zfs/killcache')
            except:
                pass

            if not os.path.exists(FAILED_FILE):
                run('cp /data/zfs/zpool.cache /data/zfs/zpool.cache.saved')
            try:
                os.unlink(IMPORTING_FILE)
            except:
                pass

            log.warn('Volume imports complete.')
            log.warn('Restarting services.')
            FREENAS_DB = '/data/freenas-v1.db'
            conn = sqlite3.connect(FREENAS_DB)
            c = conn.cursor()

            # TODO: This needs investigation.  Why is part of the LDAP
            # stack restarted?  Maybe homedir handling that
            # requires the volume to be imported?
            c.execute('SELECT ldap_enable FROM directoryservice_ldap')
            ret = c.fetchone()
            if ret and ret[0] == 1:
                run('/usr/sbin/service ix-ldap quietstart')

            # TODO: Why is lockd missing from this list?
            # why are things being restarted instead of reloaded?
            c.execute('SELECT srv_enable FROM services_services WHERE srv_service = "nfs"')
            ret = c.fetchone()
            if ret and ret[0] == 1:
                run('LD_LIBRARY_PATH=/usr/local/lib /usr/local/bin/python '
                    '/usr/local/www/freenasUI/middleware/notifier.py '
                    'nfsv4link')
                run('/usr/sbin/service ix-nfsd quietstart')
                run('/usr/sbin/service mountd reload')

            # 0 for Active node
            run('/sbin/sysctl kern.cam.ctl.ha_role=0')

            # TODO: Why is this being restarted?
            run('/usr/sbin/service ix-ssl quietstart')
            run('/usr/sbin/service ix-system quietstart')

            c.execute('SELECT srv_enable FROM services_services WHERE srv_service = "cifs"')
            ret = c.fetchone()
            if ret and ret[0] == 1:
                run('/usr/sbin/service ix-pre-samba quietstart')
                run('/usr/sbin/service samba_server forcestop')
                run('/usr/sbin/service samba_server quietstart')
                run('/usr/sbin/service ix-post-samba quietstart')

            c.execute('SELECT srv_enable FROM services_services WHERE srv_service = "afp"')
            ret = c.fetchone()
            if ret and ret[0] == 1:
                run('/usr/sbin/service ix-afpd quietstart')
                run('/usr/sbin/service netatalk forcestop')
                run('/usr/sbin/service netatalk quietstart')

            conn.close()
            log.warn('Service restarts complete.')

            # TODO: This is 4 years old at this point.  Is it still needed?
            # There appears to be a small lag if we allow NFS traffic right away. During
            # this time, we fail NFS requests with ESTALE to the remote system. This
            # gives remote clients heartburn, so rather than try to deal with the
            # downstream effect of that, instead we take a chill pill for 1 seconds.
            time.sleep(1)

            run('/sbin/pfctl -d')

            log.warn('Allowing network traffic.')
            run_async('echo "$(date), $(hostname), assume master" | mail -s "Failover" root')

            try:
                os.unlink(FAILOVER_OVERRIDE)
            except:
                pass

            run('/usr/sbin/service ix-crontab quietstart')

            # sync disks is disabled on passive node
            run('/usr/sbin/service ix-syncdisks quietstart')

            log.warn('Syncing enclosure')
            run('LD_LIBRARY_PATH=/usr/local/lib /usr/local/bin/python '
                '/usr/local/www/freenasUI/middleware/notifier.py '
                'zpool_enclosure_sync')

            run('/usr/sbin/service ix-collectd quietstart')
            run('/usr/sbin/service collectd quietrestart')
            run('/usr/sbin/service ix-syslogd quietstart')
            run('/usr/sbin/service syslog-ng quietrestart')
            run('/usr/sbin/service ix-smartd quietstart')
            run('/usr/sbin/service smartd quietrestart')

            log.warn('Failover event complete.')
    except AlreadyLocked:
        log.warn('Failover event handler failed to aquire master lockfile')


def carp_backup(fobj, state_file, ifname, vhid, event, user_override):
    log.warn("Entering BACKUP on %s", ifname)

    if not user_override:
        sleeper = fobj['timeout']
        # The specs for lagg require that if a subinterface of the lagg interface
        # changes state, all traffic on the entire logical interface will be halted
        # for two seconds while the bundle reconverges.  This means if there's a
        # toplogy change on the active node, the standby node will get a link_up
        # event on the lagg.  To  prevent the standby node from immediately pre-empting
        # we wait 2 seconds to see if the evbent was transient.
        if ifname.startswith("lagg"):
            if sleeper < 2:
                sleeper = 2
            log.warn("Sleeping %s seconds and rechecking %s", sleeper, ifname)
            # FIXME
            time.sleep(sleeper)
            error, output = run(
                "ifconfig %s | grep 'carp:' | awk '{print $2}'" % ifname
            )
            if output == 'MASTER':
                log.warn("Ignoring state on %s because it changed back to MASTER after "
                         "%s seconds.", ifname, sleeper)
                sys.exit(0)
        else:
            log.warn("Sleeping %s seconds and rechecking %s", sleeper, ifname)
            time.sleep(sleeper)
            error, output = run(
                "ifconfig %s | grep 'carp:' | awk '{print $2}'" % ifname
            )
            if output == 'MASTER':
                log.warn("Ignoring state on %s because it changed back to MASTER after "
                         "%s seconds.", ifname, sleeper)
                sys.exit(0)

    totoutput = 0
    for group, carpint in fobj['groups'].items():
        for i in carpint:
            error, output = run("ifconfig %s | grep 'carp: MASTER' | wc -l" % i)
            totoutput += int(output)

            if not error and totoutput > 0:
                log.warn(
                    'Ignoring DOWN state on %s because we still have interfaces that '
                    'are UP.', ifname)
                sys.exit(1)

    # Start the critical section
    try:
        with LockFile(FAILOVER_EVENT, timeout=0):
            # The lockfile modules cleans up lockfiles if this script exits on it's own accord.
            # For reboots, /tmp is cleared by virtue of being a memory device.
            # If someone does a kill -9 on the script while it's running the lockfile
            # will get left dangling.
            log.warn('Aquired failover backup lock')
            run('pkill -9 -f fenced')

            for group in fobj['groups']:
                for interface in fobj['groups'][group]:
                    error, output = run("ifconfig %s | grep 'carp:' | awk '{print $4}'" % interface)
                    for vhid in output.split():
                        run("ifconfig %s vhid %s advskew 100" % (interface, vhid))

            run('/sbin/pfctl -ef /etc/pf.conf.block')

            run('/usr/sbin/service watchdogd quietstop')
            run('watchdog -t 4')

            # make CTL to close backing storages, allowing pool to export
            run('/sbin/sysctl kern.cam.ctl.ha_role=1')

            # If the network is flapping, a backup node could get a master
            # event followed by an immediate backup event.  If the other node
            # is master and shoots down our master event we will immediately
            # run the code for the backup event, even though we are already backup.
            # So we use volumes as a sentinel to tell us if we did anything with
            # regards to exporting volumes.  If we don't export any volumes it's
            # ok to assume we don't need to do anything else associated with
            # transitioning to the backup state. (because we are already there)

            # Note this wouldn't be needed with a proper state engine.
            volumes = False
            for volume in fobj['volumes'] + fobj['phrasedvolumes']:
                error, output = run('zpool list %s' % volume)
                if not error:
                    volumes = True
                    log.warn('Exporting %s', volume)
                    error, output = run('zpool export -f %s' % volume)
                    if error:
                        # the zpool status here is extranious.  The sleep
                        # is going to run off the watchdog and the system will reboot.
                        run('zpool status %s' % volume)
                        time.sleep(5)
                    log.warn('Exported %s', volume)

            run('watchdog -t 0')
            try:
                os.unlink(FAILOVER_ASSUMED_MASTER)
            except:
                pass

            if volumes:
                run('/usr/sbin/service watchdogd quietstart')
                run('/usr/sbin/service ix-syslogd quietstart')
                run('/usr/sbin/service syslog-ng quietrestart')
                run('/usr/sbin/service ix-crontab quietstart')
                run('/usr/sbin/service ix-collectd quietstart')
                run('/usr/sbin/service collectd forcestop')
                run_async('echo "$(date), $(hostname), assume backup" | mail -s "Failover" root')

            if fobj['phrasedvolumes']:
                log.warn('Setting passphrase from master')
                run('LD_LIBRARY_PATH=/usr/local/lib /usr/local/sbin/enc_helper '
                    'syncfrompeer')

    except AlreadyLocked:
        log.warn('Failover event handler failed to aquire backup lockfile')


if __name__ == '__main__':

    try:
        logging.config.dictConfig({
            'version': 1,
            # 'disable_existing_loggers': True,
            'formatters': {
                'simple': {
                    'format': '[%(name)s:%(lineno)s] %(message)s',
                },
            },
            'handlers': {
                'syslog': {
                    'class': 'logging.handlers.SysLogHandler',
                    'address': '/var/run/log',
                    'formatter': 'simple',
                    'level': 'DEBUG',
                },
            },
            'loggers': {
                '': {
                    'handlers': ['syslog'],
                    'level': 'DEBUG',
                    'propagate': True,
                },
            },
        })
    except:
        logging.config.dictConfig({
            'version': 1,
            # 'disable_existing_loggers': True,
            'formatters': {
                'simple': {
                    'format': '[%(name)s:%(lineno)s] %(message)s',
                },
            },
            'handlers': {
                'console': {
                    'class': 'logging.StreamHandler',
                    'formatter': 'simple',
                    'level': 'INFO',
                    'stream': 'ext://sys.stdout',
                },
            },
            'loggers': {
                '': {
                    'handlers': ['console'],
                    'level': 'DEBUG',
                    'propagate': True,
                },
            },
        })
    log = logging.getLogger('carp.state-change-hook')

    main(*sys.argv[1:])
