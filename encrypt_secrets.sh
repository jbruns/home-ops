#!/bin/bash

docker_host=$1
deps=false
for dep in yq sops
do
    if ! command -v $dep 2>&1 >/dev/null
    then
        echo "$dep could not be found."
        deps=true
    fi
done

if $deps
then
    echo "Unsatisifed dependencies. Exiting."
    exit 1
fi

# find all docker-compose.yaml files in the directory tree. bash-3.2 compliant for macos
compose_files=()
while IFS=  read -r -d $'\0'; do
    compose_files+=("$REPLY")
done < <(find . -name "docker-compose.yaml" -print0)

# initialize encryption queue
files_to_encrypt=()

# loop the resulting compose files
for i in "${compose_files[@]}"
do
    # echo "-Evaluating $i"
    # retain qualified path to substitute for relative paths
    compose_path=${i/docker-compose.yaml/}
    
    # enumerate sops-targeted volumes within the compose file
    sops_volumes=()
    while IFS=  read ; do
        sops_volumes+=("$REPLY")
    done < <(yq '( .. | select(has("volumes")) | (.volumes[]) | select(.x-sops == "true") | (.source) )' $i)
    for x in "${sops_volumes[@]}"
    do
        # replace relative path with fully qualified    
        x=${x/.\//"$compose_path"}
        # echo "--x-sops volume: $x"
        # add this file to the encryption queue
        files_to_encrypt+=("$x")
    done

    # enumerate secrets within the compose file
    sops_secrets=()
    while IFS=  read ; do
        sops_secrets+=("$REPLY")
    done < <(yq '( .. | select(has("secrets")) | (.secrets[]) | (.file) )' $i)
    for x in "${sops_secrets[@]}"
    do
        # replace relative path with fully qualified
        x=${x/.\//"$compose_path"}
        # echo "--secret: $x"
        # add this file to the encryption queue
        files_to_encrypt+=("$x")
    done

done

# enumerate stacks
stack_names=()
while IFS=  read ; do
    stack_names+=("$REPLY")
done < <(find ./$docker_host/* -type d -maxdepth 0 -exec basename {} \;)

# add .env files
for i in "${stack_names[@]}"
do
    files_to_encrypt+=("./$docker_host/$i/.env")
done

# Main secrets operations loop
for dec_fullpath in "${files_to_encrypt[@]}"
do
    dec_extension="${dec_fullpath##*.}"
    enc_fullpath=${dec_fullpath/"$dec_extension"/"sops.$dec_extension"}
    if [ ! -f $dec_fullpath ]; then
        # decrypted file does not exist locally
        if [ -f $enc_fullpath ]; then
            echo "optional key rotation scenario"
            # encrypted file exists locally. if key rotation scenario, decrypt and re-encrypt
        else
            # neither encrypted nor decrypted files exist, but are defined in config.
            echo "WARNING: $dec_fullpath defined in Compose configuration, but is not present locally, encrypted or decrypted."
        fi
    else
        # decrypted file exists locally
        if [ -f $enc_fullpath ]; then
            # both files exist. get modified dates
            dec_mtime=$(date -r $dec_fullpath)
            enc_mtime=$(date -r $enc_fullpath)
            echo
            echo "Decrypted file: $dec_fullpath"
            echo "Last modified: $dec_mtime"
            echo
            echo "Encrypted file: $enc_fullpath"
            echo "Last modified: $enc_mtime"
            echo
            echo "Which file do you want to keep?"
            read -p "(E)ncrypted file, (D)ecrypted file, or (A)bort and do nothing? " -n 1 -r
            echo
            if [[ $REPLY =~ ^[Ee]$ ]]; then
                echo "Cleaning up decrypted file."
                rm $dec_fullpath
            elif [[ $REPLY =~ ^[Dd]$ ]]; then
                echo "Encrypting: $dec_fullpath -> $enc_fullpath"
                sops -e $dec_fullpath > $enc_fullpath; ec=$?
                if [ $ec -eq 0 ]; then
                    read -p "Remove unencrypted file? " -n 1 -r
                    if [[ $REPLY =~ ^[Yy]$ ]]; then
                        rm $dec_fullpath
                    fi
                fi
            else
                echo "Aborting."
            fi
        else
            echo "Encrypting: $dec_fullpath -> $enc_fullpath"
            sops -e $dec_fullpath > $enc_fullpath; ec=$?
            if [ $ec -eq 0 ]; then
                read -p "Remove unencrypted file? " -n 1 -r
                if [[ $REPLY =~ ^[Yy]$ ]]; then
                    rm $dec_fullpath
                fi
            fi
        fi
    fi
done

# build .gitignore per-service, per-stack, global
## per-service, per-stack: x-sops+secrets in same directory as docker-compose.yaml
## global: .env, **/.env