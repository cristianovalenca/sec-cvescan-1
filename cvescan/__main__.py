#!/usr/bin/env python3

import argparse as ap
import bz2
import cvescan.constants as const
from cvescan.errors import *
from cvescan.options import Options
from cvescan.sysinfo import SysInfo
import logging
import math
import pycurl
import os
from shutil import which,copyfile
import sys
from tabulate import tabulate

def set_output_verbosity(args):
    if args.silent:
        return get_null_logger()

    logger = logging.getLogger("cvescan.stdout")

    if args.verbose:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    log_formatter = logging.Formatter("%(message)s")
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(log_formatter)
    logger.addHandler(stream_handler)

    return logger

def get_null_logger():
    logger = logging.getLogger("cvescan.null")
    logger.addHandler(logging.NullHandler())

    return logger

LOGGER = get_null_logger()

DPKG_LOG = "/var/log/dpkg.log"
OVAL_LOG = "oval.log"
REPORT = "report.htm"
RESULTS = "results.xml"

def error_exit(msg, code=const.ERROR_RETURN_CODE):
    print("Error: %s" % msg, file=sys.stderr)
    sys.exit(code)

def download(download_url, filename):
    try:
        target_file = open(filename, "wb")
        curl = pycurl.Curl()
        curl.setopt(pycurl.URL, download_url)
        curl.setopt(pycurl.WRITEDATA, target_file)
        curl.perform()
        curl.close()
        target_file.close()
    except Exception as ex:
        raise DownloadError("Downloading %s failed: %s" % (download_url, ex))

def bz2decompress(bz2_archive, target):
    try:
        opened_archive = open(bz2_archive, "rb")
        opened_target = open(target, "wb")
        opened_target.write(bz2.decompress(opened_archive.read()))
        opened_archive.close()
        opened_target.close()
    except Exception as ex:
        raise BZ2Error("Decompressing %s to %s failed: %s", (bz2_archive, target, ex))

def parse_args():
    # TODO: Consider a more flexible solution than storing this in code (e.g. config file or launchpad query)
    acceptable_codenames = ["xenial","bionic","eoan","focal"]

    cvescan_ap = ap.ArgumentParser(description=const.CVESCAN_DESCRIPTION, formatter_class=ap.RawTextHelpFormatter)
    cvescan_ap.add_argument("-c", "--cve", metavar="CVE-IDENTIFIER", help=const.CVE_HELP)
    cvescan_ap.add_argument("-p", "--priority", help=const.PRIORITY_HELP, choices=["critical","high","medium","all"], default="high")
    cvescan_ap.add_argument("-s", "--silent", action="store_true", default=False, help=const.SILENT_HELP)
    cvescan_ap.add_argument("-o", "--oval-file", help=const.OVAL_FILE_HELP)
    cvescan_ap.add_argument("-m", "--manifest", help=const.MANIFEST_HELP,choices=acceptable_codenames)
    cvescan_ap.add_argument("-f", "--file", metavar="manifest-file", help=const.FILE_HELP)
    cvescan_ap.add_argument("-n", "--nagios", action="store_true", default=False, help=const.NAGIOS_HELP)
    cvescan_ap.add_argument("-l", "--list", action="store_true", default=False, help=const.LIST_HELP)
    cvescan_ap.add_argument("-t", "--test", action="store_true", default=False, help=const.TEST_HELP)
    cvescan_ap.add_argument("-u", "--updates", action="store_true", default=False, help=const.UPDATES_HELP)
    cvescan_ap.add_argument("-v", "--verbose", action="store_true", default=False, help=const.VERBOSE_HELP)
    cvescan_ap.add_argument("-x", "--experimental", action="store_true", default=False, help=const.EXPERIMENTAL_HELP)

    return cvescan_ap.parse_args()

def scan_for_cves(opt, sysinfo):
    run_oscap_eval(sysinfo, opt)
    run_oscap_generate_report(sysinfo.scriptdir)

    cve_list_all_filtered = run_xsltproc_all(opt.priority, sysinfo.xslt_file, opt.extra_sed)
    LOGGER.debug("%d vulnerabilities found with priority of %s or higher:" % (len(cve_list_all_filtered), opt.priority))
    LOGGER.debug(cve_list_all_filtered)

    cve_list_fixable_filtered = run_xsltproc_fixable(opt.priority, sysinfo.xslt_file, opt.extra_sed)
    LOGGER.debug("%s CVEs found with priority of %s or higher that can be " \
            "fixed with package updates:" % (len(cve_list_fixable_filtered), opt.priority))
    LOGGER.debug(cve_list_fixable_filtered)

    return (cve_list_all_filtered, cve_list_fixable_filtered)

def run_oscap_eval(sysinfo, opt):
    LOGGER.debug("Running oval scan oscap oval eval %s --results %s %s (output logged to %s/%s)" % \
            (opt.verbose_oscap_options, RESULTS, opt.oval_file, sysinfo.scriptdir, OVAL_LOG))

    # TODO: use openscap python binding instead of os.system
    return_val = os.system("oscap oval eval %s --results \"%s\" \"%s\" >%s 2>&1" % \
            (opt.verbose_oscap_options, RESULTS, opt.oval_file, OVAL_LOG))
    if return_val != 0:
        # TODO: improve error message
        raise OpenSCAPError("Failed to run oval scan: returned %d" % return_val)

def run_oscap_generate_report(scriptdir):
    LOGGER.debug("Generating html report %s/%s from results xml %s/%s " \
            "(output logged to %s/%s)" % (scriptdir, REPORT, scriptdir, RESULTS, scriptdir, OVAL_LOG))

    # TODO: use openscap python binding instead of os.system
    return_val = os.system("oscap oval generate report --output %s %s >>%s 2>&1" % (REPORT, RESULTS, OVAL_LOG))
    if return_val != 0:
        # TODO: improve error message
        raise OpenSCAPError("Failed to generate oval report: returned %d" % return_val)

    LOGGER.debug("Open %s/%s in a browser to see complete and unfiltered scan results" % (os.getcwd(), REPORT))

def run_xsltproc_all(priority, xslt_file, extra_sed):
    LOGGER.debug("Running xsltproc to generate CVE list - fixable/unfixable and filtered by priority")

    cmd = "xsltproc --stringparam showAll true --stringparam priority \"%s\"" \
          " \"%s\" \"%s\" | sed -e /^$/d %s" % (priority, xslt_file, RESULTS, extra_sed)
    cve_list_all_filtered = os.popen(cmd).read().split('\n')

    while("" in cve_list_all_filtered):
        cve_list_all_filtered.remove("")

    return cve_list_all_filtered

def run_xsltproc_fixable(priority, xslt_file, extra_sed):
    LOGGER.debug("Running xsltproc to generate CVE list - fixable and filtered by priority")

    cmd = "xsltproc --stringparam showAll false --stringparam priority \"%s\"" \
          " \"%s\" \"%s\" | sed -e /^$/d %s" % (priority, xslt_file, RESULTS, extra_sed)
    cve_list_fixable_filtered = os.popen(cmd).read().split('\n')

    while("" in cve_list_fixable_filtered):
        cve_list_fixable_filtered.remove("")

    return cve_list_fixable_filtered

def run_testmode(sysinfo, opt):
    LOGGER.info("Running in test mode.")

    if not os.path.isfile(opt.oval_file):
        raise FileNotFoundError("Missing test OVAL file at '%s', this file " \
                "should have installed with cvescan" % oval_file)

    (cve_list_all_filtered, cve_list_fixable_filtered) = scan_for_cves(opt, sysinfo)

    (results_1, success_1) = test_filter_active_cves(cve_list_all_filtered)
    (results_2, success_2) = test_identify_fixable_cves(cve_list_fixable_filtered)

    results = "%s\n%s" % (results_1, results_2)

    if not (success_1 and success_2):
        return (results, const.ERROR_RETURN_CODE)

    return (results, 0)

def test_filter_active_cves(cve_list_all_filtered):
    if ((len(cve_list_all_filtered) == 2)
            and ("CVE-1970-0300" in cve_list_all_filtered)
            and ("CVE-1970-0400" in cve_list_all_filtered)
            and ("CVE-1970-0200" not in cve_list_all_filtered)
            and ("CVE-1970-0500" not in cve_list_all_filtered)):
        return ("SUCCESS: Filter Active CVEs", True)

    return ("FAILURE: Filter Active CVEs", False)

def test_identify_fixable_cves(cve_list_fixable_filtered):
    if ((len(cve_list_fixable_filtered) == 1)
            and ("CVE-1970-0400" in cve_list_fixable_filtered)):
        return ("SUCCESS: Identify Fixable/Updatable CVEs", True)

    return ("FAILURE: Identify Fixable/Updatable CVEs", False)

def retrieve_oval_file(oval_base_url, oval_zip, oval_file):
    LOGGER.debug("Downloading %s/%s" % (oval_base_url, oval_zip))
    download(os.path.join(oval_base_url, oval_zip), oval_zip)

    LOGGER.debug("Unzipping %s" % oval_zip)
    bz2decompress(oval_zip, oval_file)

def log_config_options(opt):
    LOGGER.debug("Config Options")
    table = [
        ["Test Mode", opt.test_mode],
        ["Manifest Mode", opt.manifest_mode],
        ["Experimental Mode", opt.experimental_mode],
        ["Nagios Output Mode", opt.nagios],
        ["Target Ubuntu Codename", opt.distrib_codename],
        ["OVAL File Path", opt.oval_file],
        ["OVAL URL", opt.oval_base_url],
        ["Manifest File", opt.manifest_file],
        ["Manifest URL", opt.manifest_url],
        ["Check Specific CVE", opt.cve],
        ["CVE Priority", opt.priority],
        ["Only Show Updates Available", (not opt.all_cve)]]

    LOGGER.debug(tabulate(table))
    LOGGER.debug("")

def log_system_info(sysinfo):
    LOGGER.debug("System Info")
    table = [
        ["Local Ubuntu Codename", sysinfo.distrib_codename],
        ["Installed Package Count", sysinfo.package_count],
        ["CVEScan is a Snap", sysinfo.is_snap],
        ["$SNAP_USER_COMMON", sysinfo.snap_user_common],
        ["Scripts Directory", sysinfo.scriptdir],
        ["XSLT File", sysinfo.xslt_file]]

    LOGGER.debug(tabulate(table))
    LOGGER.debug("")

def run_manifest_mode(opt, sysinfo):
    if not opt.manifest_file:
        LOGGER.debug("Downloading %s" % opt.manifest_url)
        download(opt.manifest_url, const.DEFAULT_MANIFEST_FILE)
    else:
        copyfile(opt.manifest_file, const.DEFAULT_MANIFEST_FILE)

    package_count = count_packages_in_manifest_file(const.DEFAULT_MANIFEST_FILE)
    LOGGER.debug("Manifest package count is %s" % package_count)

    return run_cvescan(opt, sysinfo, package_count)

def count_packages_in_manifest_file(manifest_file):
    with open(manifest_file) as mf:
        package_count = len(mf.readlines())

    return package_count

def run_cvescan(opt, sysinfo, package_count):
    if opt.download_oval_file:
        retrieve_oval_file(opt.oval_base_url, opt.oval_zip, opt.oval_file)

    (cve_list_all_filtered, cve_list_fixable_filtered) = \
        scan_for_cves(opt, sysinfo)

    LOGGER.debug("Full HTML report available in %s/%s" % (sysinfo.scriptdir, REPORT))

    return analyze_results(cve_list_all_filtered, cve_list_fixable_filtered, opt, package_count)

def analyze_results(cve_list_all_filtered, cve_list_fixable_filtered, opt, package_count):
    if opt.nagios:
        return analyze_nagios_results(cve_list_fixable_filtered, opt.priority)

    if opt.cve:
        return analyze_single_cve_results(cve_list_all_filtered, cve_list_fixable_filtered, opt.cve)

    if opt.all_cve:
        return analyze_cve_list_results(cve_list_all_filtered, package_count)

    return analyze_cve_list_results(cve_list_fixable_filtered, package_count)

def analyze_nagios_results(cve_list_fixable_filtered, priority):
    if cve_list_fixable_filtered == None or len(cve_list_fixable_filtered) == 0:
        results_msg = "OK: no known %s or higher CVEs that can be fixed by updating" % priority
        return(results_msg, const.NAGIOS_OK_RETURN_CODE)

    if cve_list_fixable_filtered != None and len(cve_list_fixable_filtered) != 0:
        results_msg = ("CRITICAL: %d CVEs with priority %s or higher that can " \
                "be fixed with package updates\n%s"
                % (len(cve_list_fixable_filtered), priority, '\n'.join(cve_list_fixable_filtered)))
        return (results_msg, const.NAGIOS_CRITICAL_RETURN_CODE)

    if cve_list_all_filtered != None and len(cve_list_all_filtered) != 0:
        results_msg = ("WARNING: %s CVEs with priority %s or higher\n%s"
            % (len(cve_list_all_filtered), priority, '\n'.join(cve_list_all_filtered)))
        return (results_msg, const.NAGIOS_WARNING_RETURN_CODE)
    
    return ("UNKNOWN: something went wrong with %s" % sys.args[0], const.NAGIOS_UNKNOWN_RETURN_CODE)

def analyze_single_cve_results(cve_list_all_filtered, cve_list_fixable_filtered, cve):
    if cve in cve_list_fixable_filtered:
        return ("%s patch available to install" % cve, const.PATCH_AVAILABLE_RETURN_CODE)

    if cve in cve_list_all_filtered:
        return ("%s patch not available" % cve, const.SYSTEM_VULNERABLE_RETURN_CODE)

    return ("%s patch applied or system not known to be affected" % cve, const.SUCCESS_RETURN_CODE)

def analyze_cve_list_results(cve_list, package_count):
    results_msg = "Inspected %s packages. Found %s CVEs" % (package_count, len(cve_list))

    if cve_list != None and len(cve_list) != 0:
        results_msg = results_msg + '\n'.join(cve_list)
        return (results_msg, const.SYSTEM_VULNERABLE_RETURN_CODE)

    return (results_msg, const.SUCCESS_RETURN_CODE)

def main():
    global LOGGER

    args = parse_args()

    # Configure debug logging as early as possible
    LOGGER = set_output_verbosity(args)

    try:
        sysinfo = SysInfo(LOGGER)
    except (FileNotFoundError, PermissionError) as err:
        error_exit("Failed to determine the correct Ubuntu codename: %s" % err)
    except DistribIDError as di:
        error_exit("Invalid linux distribution detected, CVEScan must be run on Ubuntu: %s" % di)
    except PkgCountError as pke:
        error_exit("Failed to determine the local package count: %s" % pke)

    try:
        opt = Options(args, sysinfo)
    except (ArgumentError, ValueError) as err:
        error_exit("Invalid option or argument: %s" % err, const.CLI_ERROR_RETURN_CODE)

    log_config_options(opt)
    log_system_info(sysinfo)

    if sysinfo.is_snap:
        LOGGER.debug("Running as a snap, changing to '%s' directory." % sysinfo.snap_user_common)
        LOGGER.debug("Downloaded files, log files and temporary reports will " \
                "be in '%s'" % sysinfo.snap_user_common)

        try:
            os.chdir(sysinfo.snap_user_common)
        except:
            error_exit("failed to cd to %s" % sysinfo.snap_user_common)

    # TODO: Consider moving this check to SysInfo, though it may be moot if we
    #       can use python bindings for oscap and xsltproc
    if not sysinfo.is_snap:
        for i in [["oscap", "libopenscap8"], ["xsltproc", "xsltproc"]]:
            if which(i[0]) == None:
                error_exit("Missing %s command. Run 'sudo apt install %s'" % (i[0], i[1]))

    if not os.path.isfile(sysinfo.xslt_file):
        error_exit("Missing text.xsl file at '%s', this file should have installed with cvescan" % sysinfo.xslt_file)

    try:
        if opt.test_mode:
            (results, return_code) = run_testmode(sysinfo, opt)
        elif opt.manifest_mode:
            (results, return_code) = run_manifest_mode(opt, sysinfo)
        else:
            (results, return_code) = run_cvescan(opt, sysinfo, sysinfo.package_count)
    except Exception as ex:
        error_exit("An error occurred while running CVEScan: %s" % ex)

    LOGGER.info(results)
    sys.exit(return_code)

if __name__ == "__main__":
    main()