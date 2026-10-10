#!/bin/bash
# Abre el agente de vuelos en http://127.0.0.1:8792/ (arranca el servidor si hace falta)
cd "$(dirname "$0")"
if ! curl -s -o /dev/null http://127.0.0.1:8792/; then
  nohup python3 server.py > /tmp/agente-vuelos.log 2>&1 &
  sleep 1
fi
open "http://127.0.0.1:8792/"
