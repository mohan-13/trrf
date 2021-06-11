#!/bin/bash

pg_dump --version
if [ $? == 127 ]
then
    apt-get update
    apt-get install -y postgresql-client
fi

export PGPASSWORD=${POSTGRES_PASSWORD}

BACKUP_DIR="/data/backup/$(date +"%d-%m-%Y")"
mkdir -p $BACKUP_DIR
pg_dump -h db -U ${POSTGRES_USER} -Fc ${POSTGRES_USER} > ${BACKUP_DIR}/trrf_main_db.bak
pg_dump -h clinicaldb -U ${POSTGRES_USER} -Fc ${POSTGRES_USER} > ${BACKUP_DIR}/trrf_clinical_db.bak
pg_dump -h reportingdb -U ${POSTGRES_USER} -Fc ${POSTGRES_USER} > ${BACKUP_DIR}/trrf_reporting_db.bak
