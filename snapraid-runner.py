#!/usr/bin/env python3
import argparse
import logging
import logging.handlers
import os.path
import subprocess
import sys
import threading
import time
import traceback
import yaml
from collections import Counter, defaultdict

# Global variables
config = None
apprise_log_file = None


def tee_log(infile, out_lines, log_level):
    """
    Create a thread that saves all the output on infile to out_lines and
    logs every line with log_level
    """
    def tee_thread():
        for line in iter(infile.readline, ""):
            logging.log(log_level, line.rstrip())
            out_lines.append(line)
        infile.close()
    t = threading.Thread(target=tee_thread)
    t.daemon = True
    t.start()
    return t


def snapraid_command(command, args={}, *, allow_statuscodes=[]):
    """
    Run snapraid command
    Raises subprocess.CalledProcessError if errorlevel != 0
    """
    arguments = ["--conf", config["snapraid"]["config"],
                 "--quiet"]
    for (k, v) in args.items():
        arguments.extend(["--" + k, str(v)])
    p = subprocess.Popen(
        [config["snapraid"]["executable"], command] + arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Snapraid always outputs utf-8 on windows. On linux, utf-8
        # also seems a sensible assumption.
        encoding="utf-8",
        errors="replace")
    out = []
    threads = [
        tee_log(p.stdout, out, logging.OUTPUT),
        tee_log(p.stderr, [], logging.OUTERR)]
    for t in threads:
        t.join()
    ret = p.wait()
    # sleep for a while to make pervent output mixup
    time.sleep(0.3)
    if ret == 0 or ret in allow_statuscodes:
        return out
    else:
        raise subprocess.CalledProcessError(ret, "snapraid " + command)


def send_apprise_notification(success):
    try:
        from apprise import Apprise
    except ImportError:
        logging.error("Failed to send notifications because Apprise library is not installed")
        return

    appriseObj = Apprise()
    for url in config["apprise"]["urls"]:
        appriseObj.add(url)

    if success:
        body = "SnapRAID job completed successfully"
    else:
        body = "Error during SnapRAID job"

    if config["apprise"]["attach-log"]:
        appriseObj.notify(body=body, attach=apprise_log_file)
        os.remove(apprise_log_file)
    else:
        appriseObj.notify(body=body)


def finish(is_success):
    if ("error", "success")[is_success] in config["apprise"]["sendon"]:
        try:
            send_apprise_notification(is_success)
        except Exception:
            logging.exception("Failed to send notification")
    if is_success:
        logging.info("Run finished successfully")
    else:
        logging.error("Run failed")
    sys.exit(0 if is_success else 1)


def load_config(args):
    global config
    sections = ["snapraid", "logging", "apprise", "scrub"]
    config = dict((x, defaultdict(lambda: "")) for x in sections)
    
    # Loads YAML config file and adds its data to the config which is a
    # defaultdict type, this way if a key is not present it doesn't
    # raise a KeyError but defaults to an empty string
    with open(args.conf) as config_file_open:
        config_file = yaml.safe_load(config_file_open)
    for section in config_file:
        for (k, v) in config_file[section].items():
            config[section][k] = v

    # Checks if these options are of class int (or present at all)
    int_options = [
        ("snapraid", "deletethreshold"),
        ("logging", "maxsize"),
        ("scrub", "older-than"),
    ]
    for section, option in int_options:
        if not isinstance(config[section][option], int):
            config[section][option] = 0

    # Checks if these options are of class bool (or present at all)
    bool_options = [
        ("snapraid", "touch"),
        ("apprise", "short"),
        ("scrub", "enabled"),
    ]
    for section, option in bool_options:
        if not isinstance(config[section][option], bool):
            config[section][option] = False

    # Migration
    if config["scrub"]["percentage"]:
        config["scrub"]["plan"] = config["scrub"]["percentage"]

    if args.scrub is not None:
        config["scrub"]["enabled"] = args.scrub

    if args.ignore_deletethreshold:
        config["snapraid"]["deletethreshold"] = -1


def setup_logger():
    log_format = logging.Formatter(
        "%(asctime)s [%(levelname)-6.6s] %(message)s")
    root_logger = logging.getLogger()
    logging.OUTPUT = 15
    logging.addLevelName(logging.OUTPUT, "OUTPUT")
    logging.OUTERR = 25
    logging.addLevelName(logging.OUTERR, "OUTERR")
    root_logger.setLevel(logging.OUTPUT)
    console_logger = logging.StreamHandler(sys.stdout)
    console_logger.setFormatter(log_format)
    root_logger.addHandler(console_logger)

    if config["logging"]["file"]:
        max_log_size = max(config["logging"]["maxsize"], 0) * 1024
        file_logger = logging.handlers.RotatingFileHandler(
            config["logging"]["file"],
            maxBytes=max_log_size,
            backupCount=9)
        file_logger.setFormatter(log_format)
        root_logger.addHandler(file_logger)

    if config["apprise"]["attach-log"]:
        global apprise_log_file
        from tempfile import gettempdir
        from datetime import date
        apprise_log_file = os.path.join(gettempdir(), f"snapraid-runner_{date.today()}.log")
        apprise_logger = logging.FileHandler(apprise_log_file)
        apprise_logger.setFormatter(log_format)
        if config["apprise"]["short"]:
            # Don't send programm stdout in notification attachment
            apprise_logger.setLevel(logging.INFO)
        root_logger.addHandler(apprise_logger)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--conf",
                        default="snapraid-runner.yml",
                        metavar="CONFIG",
                        help="Configuration file (default: %(default)s)")
    parser.add_argument("--no-scrub", action='store_false',
                        dest='scrub', default=None,
                        help="Do not scrub (overrides config)")
    parser.add_argument("--ignore-deletethreshold", action='store_true',
                        help="Sync even if configured delete threshold is exceeded")
    args = parser.parse_args()

    if not os.path.exists(args.conf):
        print("snapraid-runner configuration file not found")
        parser.print_help()
        sys.exit(2)

    try:
        load_config(args)
    except Exception:
        print("unexpected exception while loading config")
        print(traceback.format_exc())
        sys.exit(2)

    try:
        setup_logger()
    except Exception:
        print("unexpected exception while setting up logging")
        print(traceback.format_exc())
        sys.exit(2)

    try:
        run()
    except Exception:
        logging.exception("Run failed due to unexpected exception:")
        finish(False)


def run():
    logging.info("=" * 60)
    logging.info("Run started")
    logging.info("=" * 60)

    if not os.path.isfile(config["snapraid"]["executable"]):
        logging.error("The configured snapraid executable \"{}\" does not "
                      "exist or is not a file".format(
                          config["snapraid"]["executable"]))
        finish(False)
    if not os.path.isfile(config["snapraid"]["config"]):
        logging.error("Snapraid config does not exist at " +
                      config["snapraid"]["config"])
        finish(False)

    if config["snapraid"]["touch"]:
        logging.info("Running touch...")
        snapraid_command("touch")
        logging.info("*" * 60)

    logging.info("Running diff...")
    diff_out = snapraid_command("diff", allow_statuscodes=[2])
    logging.info("*" * 60)

    diff_results = Counter(line.split(" ")[0] for line in diff_out)
    diff_results = dict((x, diff_results[x]) for x in
                        ["add", "remove", "move", "update"])
    logging.info(("Diff results: {add} added,  {remove} removed,  " +
                  "{move} moved,  {update} modified").format(**diff_results))

    if (config["snapraid"]["deletethreshold"] >= 0 and
            diff_results["remove"] > config["snapraid"]["deletethreshold"]):
        logging.error(
            "Deleted files exceed delete threshold of {}, aborting".format(
                config["snapraid"]["deletethreshold"]))
        logging.error("Run again with --ignore-deletethreshold to sync anyways")
        finish(False)

    if (diff_results["remove"] + diff_results["add"] + diff_results["move"] +
            diff_results["update"] == 0):
        logging.info("No changes detected, no sync required")
    else:
        logging.info("Running sync...")
        try:
            snapraid_command("sync")
        except subprocess.CalledProcessError as e:
            logging.error(e)
            finish(False)
        logging.info("*" * 60)

    if config["scrub"]["enabled"]:
        logging.info("Running scrub...")
        try:
            # Check if a percentage plan was given
            int(config["scrub"]["plan"])
        except ValueError:
            scrub_args = {"plan": config["scrub"]["plan"]}
        else:
            scrub_args = {
                "plan": config["scrub"]["plan"],
                "older-than": config["scrub"]["older-than"],
            }
        try:
            snapraid_command("scrub", scrub_args)
        except subprocess.CalledProcessError as e:
            logging.error(e)
            finish(False)
        logging.info("*" * 60)

    logging.info("All done")
    finish(True)


main()
