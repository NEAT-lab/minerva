#!/bin/bash

(
cd Registration_Server
source registration_env/bin/activate
python3 app.py
) > Registration_Server/registration_server.log 2>&1 &

(
cd Function_Server
source function_env/bin/activate
python3 app.py
) > Function_Server/function_server.log 2>&1 &

wait