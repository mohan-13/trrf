#!/bin/bash
#
TOPDIR=$(cd `dirname $0`; pwd)
VIRTUALENV="${TOPDIR}/virt_${PROJECT_NAME}"


# break on error
set -e 

ACTION="$1"

PROJECT_NAME='rdrf'
AWS_BUILD_INSTANCE='aws_rpmbuild_centos6'
AWS_STAGING_INSTANCE='aws-syd-rdrf-staging'
TARGET_DIR="/usr/local/src/${PROJECT_NAME}"
CLOSURE="/usr/local/closure/compiler.jar"
TESTING_MODULES="pyvirtualdisplay nose selenium lettuce lettuce_webdriver"
MODULES="psycopg2==2.5.2 Werkzeug flake8 ${TESTING_MODULES}"
PIP_OPTS='--download-cache ~/.pip/cache --process-dependency-links'


function usage() {
    echo 'Usage ./develop.sh (test|ci_lint|start|install|clean|purge|pipfreeze|pythonversion|dropdb|addusers|addregistries|ci_remote_build|ci_remote_destroy|ci_rpm_publish|ci_staging|ci_staging_selenium|ci_staging_fixture|ci_staging_tests)'
}


function settings() {
    export DJANGO_SETTINGS_MODULE="rdrf.settings"
}

# add users from json fixture
function addregistries() {
    echo "Adding RDRF registries to $HOSTNAME"
    cd /usr/local/src
    source virt_rdrf/bin/activate
    export DJANGO_SETTINGS_MODULE=rdrf.settings
    python rdrf/manage.py load_fixture --file=rdrf.json

}
function addusers {
    echo "Adding RDRF users to $HOSTNAME"
    cd /usr/local/src
    source virt_rdrf/bin/activate
    export DJANGO_SETTINGS_MODULE=rdrf.settings
    python rdrf/manage.py load_fixture --file=users.json

}

# ssh setup, make sure our ccg commands can run in an automated environment
function ci_ssh_agent() {
    ssh-agent > /tmp/agent.env.sh
    source /tmp/agent.env.sh
    ssh-add ~/.ssh/ccg-syd-staging.pem
}


# build RPMs on a remote host from ci environment
function ci_remote_build() {
    time ccg ${AWS_BUILD_INSTANCE} puppet
    time ccg ${AWS_BUILD_INSTANCE} shutdown:240

    EXCLUDES="('bootstrap'\, '.hg*'\, 'virt*'\, '*.log'\, '*.rpm'\, 'build'\, 'dist'\, '*/build'\, '*/dist')"
    SSH_OPTS="-o StrictHostKeyChecking\=no"
    RSYNC_OPTS="-l"
    time ccg ${AWS_BUILD_INSTANCE} rsync_project:local_dir=./,remote_dir=${TARGET_DIR}/,ssh_opts="${SSH_OPTS}",extra_opts="${RSYNC_OPTS}",exclude="${EXCLUDES}",delete=True
    time ccg ${AWS_BUILD_INSTANCE} build_rpm:centos/rdrf/rdrf.spec,src=${TARGET_DIR}

    mkdir -p build
    ccg ${AWS_BUILD_INSTANCE} getfile:rpmbuild/RPMS/x86_64/rdrf*.rpm,build/
}


# publish rpms 
function ci_rpm_publish() {
    time ccg publish_testing_rpm:build/rdrf*.rpm,release=6
}


# destroy our ci build server
function ci_remote_destroy() {
    ccg ${AWS_BUILD_INSTANCE} destroy
}


# puppet up staging which will install the latest rpm for each registry
function ci_staging() {
    ccg ${AWS_STAGING_INSTANCE} destroy # force recreation
    ccg ${AWS_STAGING_INSTANCE} boot
    ccg ${AWS_STAGING_INSTANCE} puppet
    ccg ${AWS_STAGING_INSTANCE} shutdown:120
}

#Preload fixtures from JSON file
function ci_staging_fixture() {
    local result=`ccg ${AWS_STAGING_INSTANCE} dsudo:'cat /tmp/rdrfsentinel || exit 0' | grep 'out: loaded' | awk  '{print $3;}'`
    echo "content of sentinel file=[$result]"
    if [ "$result" != "loaded" ]; then
        echo "/tmp/rdrfsentinel file does not exist - loading fixtures ..."
        ccg ${AWS_STAGING_INSTANCE} dsudo:'rdrf load_fixture --file\=rdrf.json'
        ccg ${AWS_STAGING_INSTANCE} dsudo:'rdrf load_fixture --file\=users.json'
        ccg ${AWS_STAGING_INSTANCE} dsudo:'echo loaded > /tmp/rdrfsentinel'
    else
        echo "Fixtures already loaded as sentinel file /tmp/rdrfsentinel exists - No fixtures were loaded"
    fi
}

# restart nginx
function restart_staging_nginx() {
    pushd /tmp
    if [ ! -d tempnginxdir ]; then
        mkdir tempnginxdir
    fi
    cd tempnginxdir
    echo nginx > .projectname
    ccg --nuke-bootstrap
    ccg ccg_syd_nginx_staging restart_nginx
    popd
}


# staging selenium test
function ci_staging_selenium() {
    ccg ${AWS_STAGING_INSTANCE} dsudo:"pip2.7 install ${PIP_OPTS} ${TESTING_MODULES}"
    ccg ${AWS_STAGING_INSTANCE} dsudo:'dbus-uuidgen --ensure'
    ccg ${AWS_STAGING_INSTANCE} dsudo:'chown apache:apache /var/www'
    ccg ${AWS_STAGING_INSTANCE} dsudo:'yum --enablerepo\=ccg-testing clean all'
    ccg ${AWS_STAGING_INSTANCE} dsudo:'yum install rdrf -y'
    ccg ${AWS_STAGING_INSTANCE} dsudo:'killall httpd || true'
    ccg ${AWS_STAGING_INSTANCE} dsudo:'service httpd start'
    ccg ${AWS_STAGING_INSTANCE} dsudo:'echo https://staging.ccgapps.com.au/rdrf > /tmp/rdrf_site_url'
    #ccg ${AWS_STAGING_INSTANCE} dsudo:'echo http://localhost/rdrf > /tmp/rdrf_site_url'
    ccg ${AWS_STAGING_INSTANCE} drunbg:"Xvfb -ac \:0"
    ccg ${AWS_STAGING_INSTANCE} dsudo:'mkdir -p lettuce && chmod o+w lettuce'
    #restart_staging_nginx
    #sleep 5
    ccg ${AWS_STAGING_INSTANCE} dsudo:"cd lettuce && env DISPLAY\=\:0 rdrf run_lettuce --with-xunit --xunit-file\=/tmp/tests.xml || true"
    ccg ${AWS_STAGING_INSTANCE} dsudo:'rm /tmp/rdrf_site_url'
    ccg ${AWS_STAGING_INSTANCE} getfile:/tmp/tests.xml,./
}

# run tests on staging
function ci_staging_tests() {
    REMOTE_TEST_DIR=/tmp
    # Grant permission to create a test database.
    DATABASE_USER=rdrf
    ccg ${AWS_STAGING_INSTANCE} dsudo:"su postgres -c \"psql -c 'ALTER ROLE ${DATABASE_USER} CREATEDB;'\""

    # This is the command which runs manage.py with the correct environment
    DJANGO_ADMIN="rdrf"

    # Run tests
    ccg ${AWS_STAGING_INSTANCE} dsudo:"cd ${REMOTE_TEST_DIR} && ${DJANGO_ADMIN} test rdrf"
}

make_virtualenv() {
    # check requirements
    which virtualenv-2.7 > /dev/null
    virtualenv-2.7 ${VIRTUALENV}
    ${VIRTUALENV}/bin/pip install ${PIP_OPTS} --upgrade 'pip>=1.5,<1.6'
}


# lint using flake8
function lint() {
    virt_rdrf/bin/flake8 rdrf --ignore=E501 --count
}

# lint js, assumes closure compiler
function jslint() {
    JSFILES="rdrf/rdrf/rdrf/static/js/*.js"
    for JS in $JSFILES
    do
        ${VIRTUALENV}/bin/gjslint --disable 0131 --max_line_length 100 --nojsdoc $JS
    done

}

# lint both Python and JS on CI server
ci_lint() {
    make_virtualenv
    ${VIRTUALENV}/bin/pip install 'closure-linter==2.3.13' 'flake8>=2.0,<2.1'
    lint
    jslint
}



# some db commands I use
function dropdb() {
    # assumes postgres, user rdrf exists, appropriate pg_hba.conf
    echo "Drop the dev database manually:"
    echo "psql -aeE -U postgres -c \"SELECT pg_terminate_backend(pg_stat_activity.procpid) FROM pg_stat_activity where pg_stat_activity.datname = 'rdrf'\" && psql -aeE -U postgres -c \"alter user rdrf createdb;\" template1 && psql -aeE -U postgres  -c \"drop database rdrf\" template1 && psql -aeE -U rdrf -c \"create database rdrf;\" template1"
}


# run the tests using nose
function nosetests() {
    source virt_rdrf/bin/activate
    virt_rdrf/bin/nosetests --with-xunit --xunit-file=tests.xml -v -w rdrf
}


# run the tests using django-admin.py
function djangotests() {
    source virt_rdrf/bin/activate
    virt_rdrf/bin/django-admin.py test rdrf --noinput
}

# nose collect, untested
function nose_collect() {
    source virt_rdrf/bin/activate
    virt_rdrf/bin/nosetests -v -w rdrf --collect-only
}


# install virt for project
function installapp() {
    # check requirements
    which virtualenv-2.7 >/dev/null

    echo "Install rdrf"
    if test -e /usr/pgsql-9.3/bin; then
        export PATH=/usr/pgsql-9.3/bin:$PATH
        echo $PATH
    fi
    virtualenv-2.7 virt_rdrf
    ./virt_rdrf/bin/pip install 'pip>=1.5,<1.6' --upgrade
    ./virt_rdrf/bin/pip --version
    pushd rdrf
    ../virt_rdrf/bin/pip install ${PIP_OPTS} -e .
    popd
    virt_rdrf/bin/pip install ${PIP_OPTS} ${MODULES}

    mkdir -p ${HOME}/bin
    ln -sf ${VIRTUALENV}/bin/python ${HOME}/bin/vpython-rdrf
}


# django syncdb, migrate and collect static
function syncmigrate() {
    echo "syncdb"
    virt_rdrf/bin/django-admin.py syncdb --noinput --settings=${DJANGO_SETTINGS_MODULE}
    echo "migrate"
    virt_rdrf/bin/django-admin.py migrate --settings=${DJANGO_SETTINGS_MODULE} 
    echo "collectstatic"
    virt_rdrf/bin/django-admin.py collectstatic --noinput --settings=${DJANGO_SETTINGS_MODULE} 1> collectstatic-develop.log
}

# start runserver
function startserver() {
    virt_rdrf/bin/django-admin.py runserver_plus 0.0.0.0:8000
}


# debug for ci
function pythonversion() {
    virt_rdrf/bin/python -V
}


# debug for ci
function pipfreeze() {
    virt_rdrf/bin/pip freeze
}


# remove pyc
function clean() {
    find rdrf -name "*.pyc" -exec rm -rf {} \;
}


# clean, delete virts and logs
function purge() {
    clean
    rm -rf virt_rdrf
    rm *.log
}


# tests
function runtest() {
    #nosetests
    djangotests
}


case ${ACTION} in
pythonversion)
    pythonversion
    ;;
pipfreeze)
    pipfreeze
    ;;
test)
    settings
    runtest
    ;;
ci_lint)
    ci_lint
    ;;
syncmigrate)
    settings
    syncmigrate
    ;;
start)
    settings
    startserver
    ;;
install)
    settings
    installapp
    ;;
ci_remote_build)
    ci_ssh_agent
    ci_remote_build
    ;;
ci_remote_destroy)
    ci_ssh_agent
    ci_remote_destroy
    ;;
ci_rpm_publish)
    ci_ssh_agent
    ci_rpm_publish
    ;;
ci_staging)
    ci_ssh_agent
    ci_staging
    ;;
ci_staging_selenium)
    ci_ssh_agent
    ci_staging_selenium
    ;;
ci_staging_fixture)
    ci_ssh_agent
    ci_staging_fixture
    ;;
ci_staging_tests)
    ci_ssh_agent
    ci_staging_tests
    ;;
addusers)
    addusers
    ;;
addregistries)
    addregistries
    ;;
dropdb)
    dropdb
    ;;
clean)
    settings
    clean 
    ;;
purge)
    settings
    clean
    purge
    ;;
*)
    usage
esac
