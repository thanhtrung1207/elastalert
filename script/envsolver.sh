#!/bin/bash

if [ -z "$1" ]; then
    echo "Usage: $0 <input-file> [output-file]"
    exit 1
fi

input_file="$1"
output_file="${2:-$input_file}"

config_file="config.env"

if [[ ! -f "$config_file" ]]; then
    echo "Error: Config file '$config_file' not found."
    exit 1
fi

source "$config_file"

if [[ ! -f "$input_file" ]]; then
    echo "Error: File '$input_file' not found."
    exit 1
fi

input_content=$(cat "$input_file")

output_content=$(echo "$input_content" | sed \
    -e "s/{{ES_HOST}}/$ES_HOST/" \
    -e "s/{{ES_PORT}}/$ES_PORT/" \
    -e "s/{{USE_SSL}}/$USE_SSL/" \
    -e "s/{{VERIFY_CERTS}}/$VERIFY_CERTS/" \
    -e "s/{{ES_USERNAME}}/$ES_USERNAME/" \
    -e "s/{{ES_PASSWORD}}/$ES_PASSWORD/")

echo "$output_content" > "$output_file"
