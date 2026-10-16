#!/bin/sh
# AnyPS5 Converter - opens the window when run without arguments
exec python3 "$(dirname "$0")/anyps5_converter.py" "$@"
