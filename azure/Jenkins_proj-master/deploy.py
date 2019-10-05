#!/usr/bin/env python3
"""
# Copyright (c) 2018, Palo Alto Networks
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

# Author: Justin Harris jharris@paloaltonetworks.com

Usage

python deploy.py --username <fwusername> -p<fwpassword> -r<resource group> -j<region>

"""

import argparse
import json
import logging
import os
import pathlib
import requests
import shutil
import subprocess
import sys
import time
import urllib3
import uuid
import xml.etree.ElementTree as ET
import xmltodict

from azure.common import AzureException
from azure.storage.file import FileService


from pandevice import firewall
from python_terraform import Terraform
from collections import OrderedDict


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_archive_dir = './WebInDeploy/bootstrap'
_content_update_dir = './WebInDeploy/content_updates/'

logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
                        datefmt='%m-%d %H:%M',
                        filename='deploy.log',
                        filemode='w')

console = logging.StreamHandler()
console.setLevel(logging.INFO)

formatter = logging.Formatter('%(name)-12s: %(levelname)-8s %(message)s')

console.setFormatter(formatter)
logging.getLogger('').addHandler(console)
logger = logging.getLogger(__name__)


# global var to keep status output
status_output = dict()

def replace_string_in_file(filepath, old_string, new_string):
    with open(filepath, 'r') as file:
        filedata = file.read()

    # Replace the target string
    filedata = filedata.replace(old_string, new_string)

    # Write the file out again
    with open(filepath, 'w') as file:
        file.write(filedata)

def move_file(src_file, dest_file):
    """

    :param src_file:
    :param dest_file:
    :return:
    """
    logger.info('Moving file {} to {}'.format(src_file,dest_file))
    try:
        shutil.copy(src_file, dest_file)
        return True
    except IOError as e:
        logger.info("Unable to copy file got error {}".format(e))
        return

def send_request(call):

    """
    Handles sending requests to API
    :param call: url
    :return: Retruns result of call. Will return response for codes between 200 and 400.
             If 200 response code is required check value in response
    """
    headers = {'Accept-Encoding' : 'None',
               'User-Agent' : 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_1) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/39.0.2171.95 Safari/537.36'}

    try:
        r = requests.get(call, headers = headers, verify=False, timeout=5)
        r.raise_for_status()
    except requests.exceptions.HTTPError as errh:
        '''
        Firewall may return 5xx error when rebooting.  Need to handle a 5xx response 
        '''
        logger.debug("DeployRequestException Http Error:")
        raise DeployRequestException("Http Error:")
    except requests.exceptions.ConnectionError as errc:
        logger.debug("DeployRequestException Connection Error:")
        raise DeployRequestException("Connection Error")
    except requests.exceptions.Timeout as errt:
        logger.debug("DeployRequestException Timeout Error:")
        raise DeployRequestException("Timeout Error")
    except requests.exceptions.RequestException as err:
        logger.debug("DeployRequestException RequestException Error:")
        raise DeployRequestException("Request Error")
    else:
        return r


class DeployRequestException(Exception):
    pass

def walkdict(dict, match):
    """
    Finds a key in a dict or nested dict and returns the value associated with it
    :param d: dict or nested dict
    :param key: key value
    :return: value associated with key
    """
    for key, v in dict.items():
        if key == match:
            jobid = v
            return jobid
        elif isinstance(v, OrderedDict):
            found = walkdict(v, match)
            if found is not None:
                return found



def update_fw(fwMgtIP, api_key):
    """
    Applies latest AppID, Threat and AV updates to firewall after launch
    :param fwMgtIP: Firewall management IP
    :param api_key: API key

    """
    # # Download latest applications and threats

    type = "op"
    cmd = "<request><content><upgrade><download><latest></latest></download></upgrade></content></request>"
    call = "https://%s/api/?type=%s&cmd=%s&key=%s" % (fwMgtIP, type, cmd, api_key)
    getjobid = 0
    jobid = ''
    key = 'job'

    # FIXME - Remove Duplicate code for parsing jobid

    while getjobid == 0:
        try:
            r = send_request(call)
            logger.info('Got response {} to request for content upgrade '.format(r.text))
        except:
            DeployRequestException
            logger.info("Didn't get http 200 response.  Try again")
        else:
            try:
                dict = xmltodict.parse(r.text)
                if isinstance(dict, OrderedDict):
                    jobid = walkdict(dict, key)
            except Exception as err:
                logger.info("Got exception {} trying to parse jobid from Dict".format(err))
            if not jobid:
                logger.info('Got http 200 response but didnt get jobid')
                time.sleep(30)
            else:
                getjobid = 1

    # FIXME - Remove Duplicate code for showing job status

    completed = 0
    while (completed == 0):
        time.sleep(45)
        call = "https://%s/api/?type=op&cmd=<show><jobs><id>%s</id></jobs></show>&key=%s" % (fwMgtIP, jobid, api_key)
        try:
            r = send_request(call)
            logger.info('Got Response {} to show jobs '.format(r.text))
        except:
            DeployRequestException
            logger.debug("failed to get jobid this time.  Try again")
        else:
            tree = ET.fromstring(r.text)
            if tree.attrib['status'] == 'success':
                try:
                    if (tree[0][0][5].text == 'FIN'):
                        logger.debug("APP+TP download Complete ")
                        completed = 1
                    print("Download latest Applications and Threats update")
                    status = "APP+TP download Status - " + str(tree[0][0][5].text) + " " + str(
                        tree[0][0][12].text) + "% complete"
                    print('{0}\r'.format(status))
                except:
                    logger.info('Checking job is complete')
                    completed = 1
            else:
                logger.info('Unable to determine job status')
                completed = 1

    # Install latest content update
    type = "op"
    cmd = "<request><content><upgrade><install><version>latest</version><commit>no</commit></install></upgrade></content></request>"
    call = "https://%s/api/?type=%s&cmd=%s&key=%s" % (fwMgtIP, type, cmd, api_key)
    getjobid = 0
    jobid = ''
    key = 'job'

    while getjobid == 0:
        try:
            r = send_request(call)
            logger.info('Got response {} to request for content upgrade '.format(r.text))
        except:
            DeployRequestException
            logger.info("Didn't get http 200 response.  Try again")
        else:
            try:
                dict = xmltodict.parse(r.text)
                if isinstance(dict, OrderedDict):
                    jobid = walkdict(dict, key)
            except Exception as err:
                logger.info("Got exception {} trying to parse jobid from Dict".format(err))
            if not jobid:
                logger.info('Got http 200 response but didnt get jobid')
                time.sleep(30)
            else:
                getjobid = 1

    completed = 0
    while (completed == 0):
        time.sleep(45)
        call = "https://%s/api/?type=op&cmd=<show><jobs><id>%s</id></jobs></show>&key=%s" % (fwMgtIP, jobid, api_key)
        try:
            r = send_request(call)
            logger.info('Got Response {} to show jobs '.format(r.text))
        except:
            DeployRequestException
            logger.debug("failed to get jobid this time.  Try again")
        else:
            tree = ET.fromstring(r.text)
            if tree.attrib['status'] == 'success':
                try:
                    if (tree[0][0][5].text == 'FIN'):
                        logger.debug("APP+TP Install Complete ")
                        completed = 1
                    print("Install latest Applications and Threats update")
                    status = "APP+TP Install Status - " + str(tree[0][0][5].text) + " " + str(
                        tree[0][0][12].text) + "% complete"
                    print('{0}\r'.format(status))
                except:
                    logger.info('Checking job is complete')
                    completed = 1
            else:
                logger.info('Unable to determine job status')
                completed = 1


    # Download latest anti-virus update without committing
    getjobid = 0
    jobid = ''
    type = "op"
    cmd = "<request><anti-virus><upgrade><download><latest></latest></download></upgrade></anti-virus></request>"
    key = 'job'
    while getjobid == 0:
        try:
            call = "https://%s/api/?type=%s&cmd=%s&key=%s" % (fwMgtIP, type, cmd, api_key)
            r = send_request(call)
            logger.info('Got response to request AV install {}'.format(r.text))
        except:
            DeployRequestException
            logger.info("Didn't get http 200 response.  Try again")
        else:
            try:
                dict = xmltodict.parse(r.text)
                if isinstance(dict, OrderedDict):
                    jobid = walkdict(dict, key)
            except Exception as err:
                logger.info("Got exception {} trying to parse jobid from Dict".format(err))
            if not jobid:
                logger.info('Got http 200 response but didnt get jobid')
                time.sleep(30)
            else:
                getjobid = 1

    completed = 0
    while (completed == 0):
        time.sleep(45)
        call = "https://%s/api/?type=op&cmd=<show><jobs><id>%s</id></jobs></show>&key=%s" % (
            fwMgtIP, jobid, api_key)
        r = send_request(call)
        tree = ET.fromstring(r.text)
        logger.debug('Got response for show job {}'.format(r.text))
        if tree.attrib['status'] == 'success':
            try:
                if (tree[0][0][5].text == 'FIN'):
                    logger.info("AV install Status Complete ")
                    completed = 1
                else:
                    status = "Status - " + str(tree[0][0][5].text) + " " + str(tree[0][0][12].text) + "% complete"
                    print('{0}\r'.format(status))
            except:
                logger.info('Could not parse output from show jobs, with jobid {}'.format(jobid))
                completed = 1
        else:
            logger.info('Unable to determine job status')
            completed = 1


def getApiKey(hostname, username, password):

    """
    Generates a Paloaltonetworks api key from username and password credentials
    :param hostname: Ip address of firewall
    :param username:
    :param password:
    :return: api_key API key for firewall
    """


    call = "https://%s/api/?type=keygen&user=%s&password=%s" % (hostname, username, password)

    api_key = ""
    while True:
        try:
            # response = urllib.request.urlopen(url, data=encoded_data, context=ctx).read()
            response = send_request(call)


        except DeployRequestException as updateerr:
            logger.info("No response from FW. Wait 20 secs before retry")
            time.sleep(10)
            continue

        else:
            api_key = ET.XML(response.content)[0][0].text
            logger.info("FW Management plane is Responding so checking if Dataplane is ready")
            logger.debug("Response to get_api is {}".format(response))
            return api_key


def getFirewallStatus(fwIP, api_key):
    fwip = fwIP

    """
    Gets the firewall status by sending the API request show chassis status.
    :param fwMgtIP:  IP Address of firewall interface to be probed
    :param api_key:  Panos API key
    """

    url = "https://%s/api/?type=op&cmd=<show><chassis-ready></chassis-ready></show>&key=%s" % (fwip, api_key)
    # Send command to fw and see if it times out or we get a response
    logger.info("Sending command 'show chassis status' to firewall")
    try:
        response = requests.get(url, verify=False, timeout=10)
        response.raise_for_status()
    except requests.exceptions.Timeout as fwdownerr:
        logger.debug("No response from FW. So maybe not up!")
        return 'no'
        # sleep and check again?
    except requests.exceptions.HTTPError as fwstartgerr:
        '''
        Firewall may return 5xx error when rebooting.  Need to handle a 5xx response
        raise_for_status() throws HTTPError for error responses
        '''
        logger.infor("Http Error: {}: ".format(fwstartgerr))
        return 'cmd_error'
    except requests.exceptions.RequestException as err:
        logger.debug("Got RequestException response from FW. So maybe not up!")
        return 'cmd_error'
    else:
        logger.debug("Got response to 'show chassis status' {}".format(response))

        resp_header = ET.fromstring(response.content)
        logger.debug('Response header is {}'.format(resp_header))

        if resp_header.tag != 'response':
            logger.debug("Did not get a valid 'response' string...maybe a timeout")
            return 'cmd_error'

        if resp_header.attrib['status'] == 'error':
            logger.debug("Got an error for the command")
            return 'cmd_error'

        if resp_header.attrib['status'] == 'success':
            # The fw responded with a successful command execution. So is it ready?
            for element in resp_header:
                if element.text.rstrip() == 'yes':
                    logger.info("FW Chassis is ready to accept configuration and connections")
                    return 'yes'
                else:
                    logger.info("FW Chassis not ready, still waiting for dataplane")
                    time.sleep(10)
                    return 'almost'


def update_status(key, value):
    """
    For tracking purposes.  Write responses to file.
    :param key:
    :param value:
    :return:
    """
    global status_output

    if type(status_output) is not dict:
        logger.info('Creating new status_output object')
        status_output = dict()

    if key is not None and value is not None:
        status_output[key] = value

    # write status to file to future tracking
    write_status_file(status_output)


def write_status_file(message_dict):
    """
    Writes the deployment state to a dict and outputs to file for status tracking
    """
    try:
        message_json = json.dumps(message_dict)
        with open('deployment_status.json', 'w+') as dpj:
            dpj.write(message_json)

    except ValueError as ve:
        logger.error('Could not write status file!')
        print('Could not write status file!')
        sys.exit(1)


def create_azure_fileshare(share_prefix, account_name, account_key):
    """
    Generate a unique share name to avoid overlaps in shared infra
    :param share_prefix:
    :param account_name:
    :param account_key:
    :return:
    """

    # FIXME - Need to remove hardcoded directoty link below

    d_dir = './WebInDeploy/bootstrap'
    share_name = "{0}-{1}".format(share_prefix.lower(), str(uuid.uuid4()))
    print('using share_name of: {}'.format(share_name))

    # archive_file_path = _create_archive_directory(files, share_prefix)

    try:
        # ignore SSL warnings - bad form, but SSL Decrypt causes issues with this
        s = requests.Session()
        s.verify = False

        file_service = FileService(account_name=account_name, account_key=account_key, request_session=s)

        # print(file_service)
        if not file_service.exists(share_name):
            file_service.create_share(share_name)

        for d in ['config', 'content', 'software', 'license']:
            print('creating directory of type: {}'.format(d))
            if not file_service.exists(share_name, directory_name=d):
                file_service.create_directory(share_name, d)

            # FIXME - We only handle bootstrap files.  May need to handle other dirs

            if d == 'config':
                for filename in os.listdir(d_dir):
                    print('creating file: {0}'.format(filename))
                    file_service.create_file_from_path(share_name, d, filename, os.path.join(d_dir, filename))

    except AttributeError as ae:
        # this can be returned on bad auth information
        print(ae)
        return "Authentication or other error creating bootstrap file_share in Azure"

    except AzureException as ahe:
        print(ahe)
        return str(ahe)
    except ValueError as ve:
        print(ve)
        return str(ve)

    print('all done')
    return share_name


def getServerStatus(IP):
    """
    Gets the server status by sending an HTTP request and checking for a 200 response code

    """
    global gcontext

    call = ("http://" + IP + "/")
    logger.info('URL request is {}'.format(call))
    # Send command to fw and see if it times out or we get a response
    count = 0
    max_count = 12
    while True:
        if count < max_count:
            time.sleep(10)
            try:
                count = count + 1
                r = send_request(call)
            except DeployRequestException as e:
                logger.debug("Got Invalid response".format(e))
            else:
                logger.info('Jenkins Server responded with HTTP 200 code')
                return 'server_up'
        else:
            break
    return 'server_down'


def apply_tf(working_dir, vars, description):

    """
    Handles terraform operations and returns variables in outputs.tf as a dict.
    :param working_dir: Directory that contains the tf files
    :param vars: Additional variables passed in to override defaults equivalent to -var
    :param description: Description of the deployment for logging purposes
    :return:    return_code - 0 for success or other for failure
                outputs - Dictionary of the terraform outputs defined in the outputs.tf file

    """
    # Set run_plan to TRUE is you wish to run terraform plan before apply
    run_plan = False
    kwargs = {"auto-approve": True}

    # Class Terraform uses subprocess and setting capture_output to True will capture output
    capture_output = kwargs.pop('capture_output', False)

    if capture_output is True:
        stderr = subprocess.PIPE
        stdout = subprocess.PIPE
    else:
        # if capture output is False, then everything will essentially go to stdout and stderrf
        stderr = sys.stderr
        stdout = sys.stdout

    start_time = time.asctime()
    print('Starting Deployment at {}\n'.format(start_time))

    # Create Bootstrap

    tf = Terraform(working_dir=working_dir)

    tf.cmd('init')
    if run_plan:

        # print('Calling tf.plan')
        tf.plan(capture_output=False)

    return_code, stdout, stderr = tf.apply(vars = vars, capture_output = capture_output,
                                            skip_plan = True, **kwargs)
    outputs = tf.output()

    logger.debug('Got Return code {} for deployment of  {}'.format(return_code, description))

    return (return_code, outputs)

def get_twistlock_console_status(mgt_ip,timeout = 5):

    url = 'https://' + mgt_ip + ':8083/api/v1/_ping'

    max_count = 30
    count = 0
    while True:
        count = count + 1
        time.sleep(30)
        if count < max_count:
            try:
                response = requests.get(url, verify=False, timeout=timeout)
                if response.status_code == 200:
                    return True
                elif response.status_code == 400:
                    return True
            except requests.exceptions.RequestException as err:
                print("General Error", err)
        else:
            return

def twistlock_signup(mgt_ip,username,password,timeout = 5):

    # $ curl - k \
    #   - H
    # 'Content-Type: application/json' \
    # - X
    # POST \
    # - d
    # '{"username": "butterbean", "password": "<PASSWORD>"}' \
    #         https: // < CONSOLE >: 8083 / api / v1 / signup
    url = 'https://' + mgt_ip + ':8083/api/v1/signup'
    payload = {
        "username": username,
        "password": password
    }
    payload = json.dumps(payload)
    headers = {'Content-Type': 'application/json'}
    max_count = 30
    count = 0
    while True:
        count = count + 1
        if count < max_count:
            try:
                response = requests.post(url, data=payload, headers=headers, verify=False, timeout=timeout)
                if response.status_code == 200:
                    return 'Success'
                elif response.status_code == 400:
                    return 'Already initialised'
            except requests.exceptions.RequestException as err:
                print("General Error", err)
        else:
            return



def twistlock_get_auth_token(mgt_ip,username,password,timeout = 5):
    """
    Generates the twistlock auth token for use in future requests
    """
    url = 'https://' + mgt_ip + ':8083/api/v1/authenticate'
    payload = {
        "username": username,
        "password": password
    }
    headers = {'Content-Type': 'application/json'}
    try:
        payload = json.dumps(payload)
        response = requests.post(url, data=payload, headers=headers, verify=False, timeout = timeout)
        response.raise_for_status()
        if response.status_code == 200:
            data = response.json()
            logger.info('Successfully generated auth token')
            return data.get('token')
    except requests.exceptions.HTTPError as err:
        logger.info('HTTP Error {}'.format(err))
        return
    except requests.exceptions.Timeout as errt:
        logger.info('Timeout Error {}'.format(errt))
        return
    except requests.exceptions.RequestException as err:
        logger.info("General Error", err)
        return

def twistlock_add_license(mgt_ip,token,license, timeout = 5):
    """Adds the license key to Twistlock console"""
    url = 'https://' + mgt_ip + ':8083/api/v1/settings/license'
    payload = {"key": license}
    Bearer = "Bearer " + token
    headers = {'Content-Type': 'application/json',
               'Authorization': Bearer }
    logger.info('Url is {}\n Payload is {}\n Headers is{}'.format(url,payload,headers))
    try:
        payload = json.dumps(payload)
        response = requests.post(url, data=payload, headers=headers, verify=False, timeout = timeout)
        response.raise_for_status()
        if response.status_code == 200:
            logger.info('Successfully added license')
            return 'Success'
    except requests.exceptions.HTTPError as err:
        logger.info('HTTP Error {}'.format(err))
        return
    except requests.exceptions.Timeout as errt:
        logger.info('Timeout Error {}'.format(errt))
        return
    except requests.exceptions.RequestException as err:
        logger.info("General Error", err)
        return

def get_twistlock_policy_from_console(mgt_ip, token, timeout = 5):
    """
    Retrieves existing container policy as a list.
    In order to update policies first download as a list and insert reomve
    lines of policy

    """
    url = 'https://' + mgt_ip + ':8083/api/v1/policies/runtime/container'


    Bearer = "Bearer " + token
    headers = {'Content-Type': 'application/json',
               'Authorization': Bearer}
    try:
        response = requests.get(url, headers= headers, verify=False, timeout=timeout)
        if response.status_code == 200:
            return response.content
        elif response.status_code == 400:
            return
    except requests.exceptions.RequestException as err:
        print("General Error", err)

def get_twistlock_policy_from_file(filename):
    """
    loads the Jenkins policy from file:
    """
    with open(filename) as file:
        json_rule = json.loads(file.read())

        return(json_rule)

def update_twistlock_policy(mgt_ip, token, policy, timeout=5):
    """
    Use put operation to update teh policy on the server
    """
    url = 'https://' + mgt_ip + ':8083/api/v1/policies/runtime/container'
    data = policy

    Bearer = "Bearer " + token
    headers = {'Content-Type': 'application/json',
               'Authorization': Bearer}
    try:
        response = requests.put(url, headers=headers, data=json.dumps(data), verify=False, timeout=timeout)
        if response.status_code == 200:
            return True
        else:
            return
    except requests.exceptions.RequestException as err:
        return

def check_http_link(path):
    """
    Checks that the URL is valid
    """
    r = requests.head(path)
    return r.status_code == requests.codes.ok

def main(username, password, rg_name, azure_region,twistlock_license_key, cdn_url):

    """
    Main function
    :param username:
    :param password:
    :param rg_name: Resource group name prefix
    :param azure_region: Region
    :return:
    """
    username = username
    password = password

    WebInBootstrap_vars = {
        'RG_Name': rg_name,
        'Azure_Region': azure_region
    }

    WebInDeploy_vars = {
        'Admin_Username': username,
        'Admin_Password': password,
        'Azure_Region': azure_region
    }

    WebInFWConf_vars = {
        'Admin_Username': username,
        'Admin_Password': password
    }

    # Set run_plan to TRUE is you wish to run terraform plan before apply
    run_plan = True
    kwargs = {"auto-approve": True}

    #   File constants.
    #   We are using template files for the webserver instance and console.
    #   At the start of the run we copy the template file to the tf and then populate values with string replacement
    #   Ensures that on each run we can update the tf file with latest input parameters

    console_filename = './TwistlockDeploy/scripts/initialize_console.sh'
    console_template = './TwistlockDeploy/scripts/initialize_console.templ'
    jenkins_policy_filename = './TwistlockDeploy/twistlock_rule.js'

    #
    #   Are we deploying Twistlock on this run?
    #   Do we have a license string and a reachable link?
    #

    if twistlock_license_key and cdn_url:

        if check_http_link(cdn_url):
            logger.info('Link to tar file is valid. Lets continue')
        else:
            logger.info('The specified link to the Twistlock install software is not reachable')
            sys.exit(1)

        TwistlockDeploy_vars = {
          'Admin_Username': username,
          'Admin_Password': password,
          'Azure_Region': azure_region,
          'RG_Name': rg_name
        }
    #
            # Setup the install script again
        # Runs every time to pick up new version of code if required.
        #

        string_to_match = '<cdn-url>'
        fcopy1=move_file(console_template, console_filename)
        fcopy2=move_file(webservers_with_console_template, webservers_filename)

        if fcopy1 and fcopy2:
            logger.info('Created new console and webserver tf files')
        else:
            logger.info('Unable to create new console.tf and webservers.tf files')
            sys.exit(1)


        replace_string_in_file(console_filename, string_to_match, cdn_url)

        return_code, console_deploy_output = apply_tf('./TwistlockDeploy', TwistlockDeploy_vars, 'TwistlockDeploy')

        logger.debug('Got Return code for deploy TwistlockDeploy {}'.format(return_code))

        # update_status('web_in_deploy_stdout', stdout)
        update_status('console_deploy_output', console_deploy_output)

        if return_code == 0:
            update_status('console_deploy_output', 'success')
            console_mgt_ip = console_deploy_output['CONSOLE-MGT']['value']

            #
            # Replace cdn url in console setup file with latest version
            #
        #
        # Setup Twistlock Console
        #
        if get_twistlock_console_status(console_mgt_ip):
            # Create initial user account
            twistlock_signup(console_mgt_ip, username, password)
            # Generate auth token from credentials
            token = twistlock_get_auth_token(console_mgt_ip, username, password)
            # Add license key
            license_add_response = twistlock_add_license(console_mgt_ip, token, twistlock_license_key)
            # Download container policy from console

            jenkins_policy = get_twistlock_policy_from_file(jenkins_policy_filename)

            # Upload additional Jenkins policy to server
            update_twistlock_policy(console_mgt_ip, token, jenkins_policy)

        else:
            sys.exit(1)

        if license_add_response == 'Success':
            logger.info("Twistlock Console licensed and Ready")


        #
        # Now the console is up we can setup the webserver tf file
        # the webserser.tf file will now has script to register with console using auth token

        replace_string_in_file(webservers_filename, '<CONSOLE>', console_mgt_ip)
        replace_string_in_file(webservers_filename, '<AUTHKEY>', token)

    else:
        logger.info('No twistlock install this time')
        move_file(webservers_template, webservers_filename)

    return_code, outputs = apply_tf('./WebInBootstrap',WebInBootstrap_vars, 'WebInBootstrap')

    if return_code == 0:
        share_prefix = 'jenkins-demo'
        resource_group = outputs['Resource_Group']['value']
        bootstrap_bucket = outputs['Bootstrap_Bucket']['value']
        storage_account_access_key = outputs['Storage_Account_Access_Key']['value']
        update_status('web_in_bootstrap_status', 'success')
    else:
        logger.info("WebInBootstrap failed")
        update_status('web_in_bootstap_status', 'error')
        print(json.dumps(status_output))
        exit(1)


    share_name = create_azure_fileshare(share_prefix, bootstrap_bucket, storage_account_access_key)

    WebInDeploy_vars.update({'Storage_Account_Access_Key': storage_account_access_key})
    WebInDeploy_vars.update({'Bootstrap_Storage_Account': bootstrap_bucket})
    WebInDeploy_vars.update({'RG_Name': resource_group})
    WebInDeploy_vars.update({'Attack_RG_Name': resource_group})
    WebInDeploy_vars.update({'Storage_Account_Fileshare': share_name})

    #
    # Build Infrastructure
    #
    #


    return_code, web_in_deploy_output = apply_tf('./WebInDeploy', WebInDeploy_vars, 'WebInDeploy')

    logger.debug('Got Return code for deploy WebInDeploy {}'.format(return_code))


    update_status('web_in_deploy_output', web_in_deploy_output)
    if return_code == 0:
        update_status('web_in_deploy_status', 'success')
        albDns = web_in_deploy_output['ALB-DNS']['value']
        fwMgt = web_in_deploy_output['MGT-IP-FW-1']['value']
        nlbDns = web_in_deploy_output['NLB-DNS']['value']
        fwMgtIP = web_in_deploy_output['MGT-IP-FW-1']['value']

        logger.info("Got these values from output of WebInDeploy \n\n")
        logger.info("AppGateway address is {}".format(albDns))
        logger.info("Internal loadbalancer address is {}".format(nlbDns))
        logger.info("Firewall Mgt address is {}".format(fwMgt))

    else:
        logger.info("WebInDeploy failed")
        update_status('web_in_deploy_status', 'error')
        print(json.dumps(status_output))
        exit(1)

    #
    # Check firewall is up and running
    #
    #

    api_key = getApiKey(fwMgtIP, username, password)

    while True:
        err = getFirewallStatus(fwMgtIP, api_key)
        if err == 'cmd_error':
            logger.info("Command error from fw ")

        elif err == 'no':
            logger.info("FW is not up...yet")
            # print("FW is not up...yet")
            time.sleep(60)
            continue

        elif err == 'almost':
            logger.info("MGT up waiting for dataplane")
            time.sleep(20)
            continue

        elif err == 'yes':
            logger.info("FW is up")
            break

    logger.debug('Giving the FW another 10 seconds to fully come up to avoid race conditions')
    time.sleep(10)
    fw = firewall.Firewall(hostname=fwMgtIP, api_username=username, api_password=password)


    logger.info("Updating firewall with latest content pack")
    update_fw(fwMgtIP, api_key)

    #
    # Configure Firewall
    #
    WebInFWConf_vars.update({'FW_Mgmt_IP': fwMgtIP})

    logger.info("Applying addtional config to firewall")

    return_code, web_in_fw_conf_out = apply_tf('./WebInFWConf', WebInFWConf_vars, 'WebInFWConf')

    if return_code == 0:
        update_status('web_in_fw_conf', 'success')
        logger.info("WebInFWConf ok")

    else:
        logger.info("WebInFWConf sent return code {}".format(return_code))
        update_status('web_in_deploy_status', 'error')
        print(json.dumps(status_output))
        exit(1)

    logger.info("Commit changes to firewall")

    fw.commit()
    logger.info("waiting for commit")
    time.sleep(60)
    logger.info("waiting for commit")

    #
    # Check Jenkins
    #

    logger.info('Checking if Jenkins Server is ready')

    res = getServerStatus(albDns)

    if res == 'server_up':
        logger.info('Jenkins Server is ready')
        logger.info('\n\n   ### Deployment Complete ###')
        logger.info('\n\n   Connect to Jenkins Server at http://{}'.format(albDns))
    else:
        logger.info('Jenkins Server is down')
        logger.info('\n\n   ### Deployment Complete ###')

    # dump out status to stdout
    print(json.dumps(status_output))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Get Terraform Params')
    parser.add_argument('-u', '--username', help='Firewall Username', required=True)
    parser.add_argument('-p', '--password', help='Firewall Password', required=True)
    parser.add_argument('-r', '--resource_group', help='Resource Group', required=True)
    parser.add_argument('-j', '--azure_region', help='Azure Region', required=True)
    parser.add_argument('-v', '--twistlock_key', help='Twistlock license key',required=False)
    parser.add_argument('-t', '--twistlock_url', help='URL for latest twistlock version',required=False)

    args = parser.parse_args()
    username = args.username
    password = args.password
    resource_group = args.resource_group
    azure_region = args.azure_region
    if args.twistlock_key is not None:
        twistlock_license_key=args.twistlock_key
    else:
        twistlock_license_key=False
    if args.twistlock_url is not None:
        cdn_url = args.twistlock_url
    else:
        cdn_url=False

    main(username, password, resource_group, azure_region, twistlock_license_key, cdn_url)
