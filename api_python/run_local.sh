#!/bin/bash

# Configuration for local testing of sqlite-backend
export POSTINGS_IMAGINATION_DB="/Users/larsj/Github/Dash_Imagination/src/dash_imagination/data/imagination.db"
export POSTINGS_CONFIG="/Users/larsj/Github/sqlite-backend/api_python/config_local.json"

echo "Oppstarter lokal API på port 8080..."
echo "Bruker databasen: $POSTINGS_IMAGINATION_DB"

# Sjekk om uvicorn er installert
if ! command -v uvicorn &> /dev/null
then
    echo "Feil: uvicorn er ikke installert. Vennligst kjør: pip install uvicorn"
    exit 1
fi

uvicorn server:app --reload --port 8080 --host 0.0.0.0
