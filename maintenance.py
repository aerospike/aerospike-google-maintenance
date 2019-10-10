#!/usr/bin/env python

# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Example of using the Compute Engine API to watch for maintenance notices.

For more information, see the README.md under /compute.
"""

# [START all]

import argparse
import time
import os

import requests
import logging

import subprocess

METADATA_URL = 'http://metadata.google.internal/computeMetadata/v1/'
METADATA_HEADERS = {'Metadata-Flavor': 'Google'}
ASINFO = "/usr/bin/asinfo"
ASADM = "/usr/bin/asadm"
AGM_LOG = "/var/log/aerospike/agm.log"
AGM_LEVEL = logging.INFO
# Timeout in seconds, up to 3600 (which is google max timeout)
MAX_TIMEOUT = 3600

# persist status over runs
is_persistent_last_event = False  # the default set at the parser
AS_TMP_LAST_STATUS_FILE = '/tmp/agm_last_status.tmp'

# logger setup
logging.basicConfig()
logger = logging.getLogger('AGM')
logger.setLevel(AGM_LEVEL)

f_handler = logging.FileHandler(AGM_LOG)
f_format = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
f_handler.setFormatter(f_format)

logger.addHandler(f_handler)

# option parser
parser = argparse.ArgumentParser()
parser.add_argument("-o",
                    "--options",
                    dest = "options",
                    default = "",
                    help = "Additional options to pass into asinfo. Can be anything except commands, ie: \"-v $COMMAND\". Entire string must be quoted, eg: -o=\"-u admin -p admin\"")

parser.add_argument("-t",
                    "--timeout",
                    type=int,
                    dest="timeout",
                    default=3600,
                    help="Timeout for the Google metadata service in seconds, up to 3600 (default 3600)")

feature_parser = parser.add_mutually_exclusive_group(required=False)
feature_parser.add_argument("-p",
                    "--persist",
                    dest="is_persistent_last_event",
                    action="store_true",
                    help="Persist the last event to file")

feature_parser.add_argument("-n",
                    "--non-persist",
                    dest="is_persistent_last_event",
                    action="store_false",
                    help="Disable persist the last event to file (default)")

parser.set_defaults(is_persistent_last_event=False)
args = parser.parse_args()

logger.debug('options: %s', args.options)
logger.debug('is_persistent_last_event: %s', args.is_persistent_last_event)
logger.debug('max_timeout: %s', args.timeout)

is_persistent_last_event = args.is_persistent_last_event
MAX_TIMEOUT = args.timeout

# Persist status over runs
def set_last_maintenance_event(last_maintenance_event):
    try:
        with open(AS_TMP_LAST_STATUS_FILE, 'w') as tmpfile:
            tmpfile.write(str(last_maintenance_event))
    except IOError as e:
        logger.error('Could not open %s for writing: %s', AS_TMP_LAST_STATUS_FILE, str(e))

def get_last_maintenance_event():

    if not os.path.isfile(AS_TMP_LAST_STATUS_FILE):
        return "NONE"

    try:
        with open(AS_TMP_LAST_STATUS_FILE, 'r') as tmpfile:
            last_status = tmpfile.read()
            return last_status

    except IOError as e:
        logger.error("Could not open %s for reading: %s", AS_TMP_LAST_STATUS_FILE, str(e))
        return "NONE"

# Run shell command and check success or failure.
def run_shell_command(command):
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = p.communicate()

    if stderr:
        logger.error("Command: \"" + " ".join(command) + "\" Error: \n" + stderr)
        return

    if stdout:
        logger.info("Command: \"" + " ".join(command) + "\" Output: \n" + stdout)

    if p.returncode != 0:
        logger.error("Command: \"" + " ".join(command) + "\" returned with error code: " + str(p.returncode))
    else:
        logger.info("Command \"" + " ".join(command) + "\" ran successfully")


def wait_for_maintenance(callback):
    url = METADATA_URL + 'instance/maintenance-event'
    if is_persistent_last_event:
        last_maintenance_event = get_last_maintenance_event()
    else:
        last_maintenance_event = "NONE"

    # [START hanging_get]
    last_etag = '0'

    while True:
        logger.info("getting the metadata request, waiting for event")
        try:
            r = requests.get(
                url,
                params={'last_etag': last_etag, 'wait_for_change': True, 'timeout_sec': MAX_TIMEOUT},
                headers=METADATA_HEADERS)
            logger.info("Google Metadata returned with status code: %s. text: %s", r.status_code, r.text.encode('utf-8').strip())

        except requests.exceptions.TooManyRedirects as tmr:
            # A request exceeds the configured number of maximum redirections, stop script
            # May want to try a different URL, better not to retry.
            logger.error("Too Many Redirects %s, terminating!", str(tmr))
            raise tmr

        except requests.exceptions.RequestException as re:
            # Retry for all other errors (ConnectionError, Timeout ..).
            # The check for 503 and raise_for_status() - 4XX and 5XX will be done separately.
            # https://2.python-requests.org//en/latest/user/quickstart/#errors-and-exceptions
            logger.error("Request Exception %s, Retrying....", str(re))
            time.sleep(1)
            continue

        # During maintenance the service can return a 503, so these should be retried.
        # Check https://cloud.google.com/compute/docs/storing-retrieving-metadata#statuscodes
        if r.status_code == 503:
            time.sleep(1)
            continue

        # Any other response from metadata service will kill this script.
        # No retry.
        try:
            r.raise_for_status()
        except requests.exceptions.HTTPError as error:
            logger.error("metadata service returned bad code, terminating! (%s)", str(error))
            raise error

        last_etag = r.headers['etag']
        # [END hanging_get]

        if r.text == 'NONE':
            maintenance_event = 'NONE'
        else:
            # Possible events:
            #   MIGRATE_ON_HOST_MAINTENANCE: instance will be migrated
            #   TERMINATE_ON_HOST_MAINTENANCE: instance will be shut down
            maintenance_event = r.text.encode('utf-8').strip()

        if is_persistent_last_event:
            last_maintenance_event = get_last_maintenance_event()

        if maintenance_event != last_maintenance_event:
            logger.info("Maintenance event changed from %s to %s", last_maintenance_event, maintenance_event)
            last_maintenance_event = maintenance_event
            if is_persistent_last_event:
                set_last_maintenance_event(last_maintenance_event)

            callback(maintenance_event)


def maintenance_callback(event):
    if event != "NONE":
        logger.warning('Undergoing host maintenance: %s', event)
        # realistically, any sort of maintenance event should drain aerospike
        asinfo = [ASINFO, "-v", "quiesce:"]
        asinfo.extend(args.options.split())
        run_shell_command(asinfo)
        logger.info('quiesce finished')

    else:
        logger.info('Finished host maintenance')
        asinfo = [ASINFO, "-v", "quiesce-undo:"]
        asinfo.extend(args.options.split())
        run_shell_command(asinfo)
        logger.info('quiesce-undo finished')

    asadm = [ASADM, "-e", "asinfo -v \"recluster:\""]
    asadm.extend(args.options.split())
    run_shell_command(asadm)
    logger.info('recluster finished')

def main():
    wait_for_maintenance(maintenance_callback)


if __name__ == '__main__':
    logger.info('init service')
    main()
# [END all]

