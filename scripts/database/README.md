# Backing Up & Restoring Databases
We have two shell scripts that help in backing up and restoring databases.

> These scripts and the commnds metioned below works only with application running using docker-compose-prod-with-local-db.yml

## To Backup database:

- Login to server using SSH
- Make sure TRRF is running by executing:

    ``` docker ps ```


- Execute the following command: 

    ``` docker exec -u root -it trrf_application_1 /bin/bash /app/scripts/db_backup.sh ```

- This will create a folder with the current date in the __/data/backup__ inside the containter and the backup files of all three databases will be created.
- The data directory is mounted as volume to the host machine with the data folder inside the application source directory. i.e Backup files can be found in the host machine under ~/trrf/data/prod/backup/_\<backup-date>_/ directory.


## To Restore database from a backup:

- Login to server using SSH
- Make sure TRRF is running by executing:

    ``` docker ps ```


- Execute the following command by replacing the last parameter with backup date :

     ``` docker exec -u root -it trrf_application_1 /bin/bash /app/scripts/db_restore.sh 09-06-2021 ```



- The above command will make a clean restore of the databases to the backup state.


