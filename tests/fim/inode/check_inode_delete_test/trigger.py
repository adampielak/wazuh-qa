# Trigger script.
# This script generate the event that we need to analyse in the manager.

import os

file = "/fim_test/check_inode_file.txt"

if os.path.exists(file):
  os.remove(file)