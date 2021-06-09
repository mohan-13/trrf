#!/bin/bash

pg_restore --version
if [ $? == 127 ]
then
    apt-get update
    apt-get install -y postgresql-client
fi
if [[ $# == 0 || ! "$1" =~ ^[0-3][0-9]-[0-1][1-9]-[0-9][0-9][0-9][0-9]$ ]]
then
    echo "Restore takes a date parameter of format DD_MM_YYYY"
    exit 2
fi

if [ ! -d "/data/backup/$1" ]
then
    echo "Backup for the date: $1 not found."
    exit 2
fi

export PGPASSWORD=${POSTGRES_PASSWORD}
pg_restore -h db -U ${POSTGRES_USER} -d ${POSTGRES_USER} --clean /data/backup/$1/trrf_main_db.bak
pg_restore -h clinicaldb -U ${POSTGRES_USER} -d ${POSTGRES_USER} --clean /data/backup/$1/trrf_clinical_db.bak
pg_restore -h reportingdb -U ${POSTGRES_USER} -d ${POSTGRES_USER} --clean /data/backup/$1/trrf_reporting_db.bak
