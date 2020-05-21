# Copyright (C) 2015-2020, Wazuh Inc.
# Created by Wazuh, Inc. <info@wazuh.com>.
# This program is free software; you can redistribute it and/or modify it under the terms of GPLv2

import os
import pytest
import subprocess
import yaml
import socket 

from wazuh_testing.tools.configuration import load_wazuh_configurations
from wazuh_testing.tools.configuration import get_wazuh_conf, set_section_wazuh_conf, write_wazuh_conf
from wazuh_testing.tools.enrollment import EnrollmentSimulator
from wazuh_testing.tools.monitoring import QueueMonitor
from wazuh_testing.tools.services import control_service
from wazuh_testing.fim import generate_params
from conftest import DEFAULT_VALUES, SERVER_KEY_PATH, SERVER_CERT_PATH, build_expected_request, clean_client_keys_file, check_client_keys_file, clean_password_file, \
    configure_enrollment
# Marks

pytestmark = [pytest.mark.linux, pytest.mark.tier(level=0), pytest.mark.agent]

SERVER_ADDRESS = '127.0.0.1'
REMOTED_PORT = 1514
INSTALLATION_FOLDER = '/var/ossec/bin/'


def load_tests(path):
    """ Loads a yaml file from a path 
    Retrun 
    ----------
    yaml structure
    """
    with open(path) as f:
        return yaml.safe_load(f)


test_data_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'data')
configurations_path = os.path.join(test_data_path, 'wazuh_conf.yaml')
tests = load_tests(os.path.join(test_data_path, 'wazuh_enrollment_tests.yaml'))

#params = [{'SERVER_ADDRESS': SERVER_ADDRESS,}, {'PORT': REMOTED_PORT,},]

params = [{'SERVER_ADDRESS': SERVER_ADDRESS,}]
metadata = [{}]
configurations = load_wazuh_configurations(configurations_path, __name__, params=params, metadata=metadata)

enrollment_server = EnrollmentSimulator(server_address=SERVER_ADDRESS, remoted_port=REMOTED_PORT, key_path=SERVER_KEY_PATH, cert_path=SERVER_CERT_PATH)

receiver_sockets, monitored_sockets, log_monitors = None, None, None  # Set in the fixtures

# fixtures
@pytest.fixture(scope="module", params=configurations)
def get_configuration(request):
    """Get configurations from the module"""
    return request.param

@pytest.fixture(scope="module")
def configure_enrollment_server(request):
    enrollment_server.start()
    global monitored_sockets
    monitored_sockets = [QueueMonitor(x) for x in enrollment_server.queues]

    yield

    enrollment_server.shutdown()

def override_wazuh_conf(configuration):
    # Stop Wazuh
    control_service('stop')
    
    
    # Configuration for testing
    temp = get_temp_yaml(configuration)
    conf = load_wazuh_configurations(temp, __name__,)
    os.remove(temp)
    
    
    test_config = set_section_wazuh_conf(conf[0]['sections'])
    # Set new configuration
    write_wazuh_conf(test_config)

    
    #reset_client_keys
    clean_client_keys_file()
    clean_password_file()
    #reset password
    #reset_password(set_password)

    # Start Wazuh
    control_service('start')

def get_temp_yaml(param):
    temp = os.path.join(test_data_path,'temp.yaml')
    with open(configurations_path , 'r') as conf_file:
        auto_enroll_conf = {'auto_enrollment' : {'elements' : []}}
        for elem in param:
            auto_enroll_conf['auto_enrollment']['elements'].append({elem : {'value': param[elem]}})
        print(auto_enroll_conf)
        temp_conf_file = yaml.safe_load(conf_file)
        temp_conf_file[0]['sections'][0]['elements'].append(auto_enroll_conf)
    with open(temp, 'w') as temp_file:
        yaml.safe_dump(temp_conf_file, temp_file)
    return temp
        


@pytest.mark.parametrize('test_case', [case for case in tests])
def test_agent_agentd_enrollment(configure_enrollment_server, configure_environment, test_case: list):
    print(f'Test: {test_case["name"]}')
    if 'ossec-agentd' in test_case.get("skips", []):
        pytest.skip("This test does not apply to ossec-agentd")
    configuration = test_case.get('configuration', {})
    configure_enrollment(test_case.get('enrollment'), enrollment_server, configuration.get('agent_name'))
    try:
        override_wazuh_conf(configuration)
    except:
        if not test_case.get('enrollment',{}).get('response'):
            # Expected to happen
            return
        else:
            raise AssertionError(f'Configuration error at ossec.conf file')

    if test_case.get('enrollment') and test_case['enrollment'].get('response'):
        #configuration = test_case.get('configuration', {})
        results = monitored_sockets[0].get_results(callback=(lambda y: [x.decode() for x in y]), timeout=1, accum_results=1)
        assert results[0] == build_expected_request(configuration), 'Expected enrollment request message does not match'
        assert results[1] == test_case['enrollment']['response'].format(**DEFAULT_VALUES), 'Expected response message does not match'
        assert check_client_keys_file(results[1]) == True, 'Client key does not match'
    else:
        raise AssertionError(f'Will be configuration error at ossec.conf file')
    return