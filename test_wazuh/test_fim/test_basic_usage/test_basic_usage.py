# Copyright (C) 2015-2019, Wazuh Inc.
# Created by Wazuh, Inc. <info@wazuh.com>.
# This program is free software; you can redistribute it and/or modify it under the terms of GPLv2
import os
import time
from datetime import timedelta
import glob
import pytest

from wazuh_testing.fim import callback_detect_end_scan, callback_detect_event, LOG_FILE_PATH
from wazuh_testing.tools import TimeMachine, FileMonitor


test_data_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'data')
test_directories = [os.path.join('/', 'testdir1'), os.path.join('/', 'testdir2')]
testdir1, testdir2 = test_directories

wazuh_log_monitor = FileMonitor(LOG_FILE_PATH)


@pytest.fixture(scope='module', params=glob.glob(os.path.join(test_data_path, 'ossec*.conf')))
def get_ossec_configuration(request):
    return request.param


@pytest.mark.parametrize('folder, filename, mode, content', [
    (testdir1, 'testfile', 'w', "Sample content"),
    (testdir1, 'btestfile', 'wb', b"Sample content"),
    (testdir2, 'testfile', 'w', ""),
    (testdir2, "btestfile", "wb", b"")
])
def test_regular_file(folder, filename, mode, content, configure_environment, restart_wazuh):
    """Checks if a regular file creation is detected by syscheck"""

    # Create text files
    with open(os.path.join(folder, filename), mode) as f:
        f.write(content)

    # Go ahead in time to let syscheck perform a new scan
    print("Muevo el reloj 13 horas al futuro")
    TimeMachine.travel_to_future(timedelta(hours=13))

    # Wait until event is detected
    print("Espero a que salte el evento")
    wazuh_log_monitor.start(timeout=10, callback=callback_detect_event)

    # Wait for FIM scan to finish
    print("Espero a que termine el scan")
    wazuh_log_monitor.start(timeout=10, callback=callback_detect_end_scan)
    print("Espero 11 segundos")
    time.sleep(11)