import os
import shutil

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
dot_venv = os.path.join(backend_dir, '.venv')
venv = os.path.join(backend_dir, 'venv')

if os.path.exists(dot_venv):
    if os.path.exists(venv):
        shutil.rmtree(venv)
    os.rename(dot_venv, venv)
    print("Successfully renamed virtual environment folder from '.venv' to 'venv'!")
elif os.path.exists(venv):
    print("Virtual environment folder is already named 'venv'.")
else:
    print("No virtual environment folder found. Run 'make setup' to create 'venv'.")
