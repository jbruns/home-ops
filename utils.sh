#!/bin/bash
for argument in "$@"
do
  key=$(echo ${argument/--/""} | cut -f1 -d=)
  value=$(echo $argument | cut -f2 -d=)

  case "$key" in
    "host")              docker_host="$value" ;;
    "rotate-keys")       rotate_keys=true ;;
    *)
  esac
done

if [ ! $docker_host ]; then
    echo "Target Docker host not specified (--host=someNameHere)."
    exit 1
fi

if [ $rotate_keys ]; then
    echo "Rotate keys specified. This will decrypt "
fi

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

# enumerate stacks
stack_names=()
while IFS=  read ; do
    stack_names+=("$REPLY")
done < <(find ./$docker_host/* -maxdepth 0 -type d -exec basename {} \;)

# construct and queue encryption for env files
for i in "${stack_names[@]}"
do
    # add .env files to encryption queue
    files_to_encrypt+=("./$docker_host/$i/.env")
    # enumerate unique instances of environment variable interpolation in the stack
    env_names=()
    while IFS=  read ; do
        env_names+=("$REPLY")
    done < <(grep -ho -e '\${[A-Z0-9_-]*}' ./$docker_host/$i/**/docker-compose.yaml | sort -u)
    # write env.template
    for x in "${env_names[@]}"
    do
        # strip to just the variable name
        x=$(echo "$x" | tr -d "$\{\}")
        if ! grep -Fxq "$x=" ./$docker_host/$i/env.template; then
            echo "adding $x to env.template for $i stack"
            echo "$x=" >> ./$docker_host/$i/env.template
        fi
    done
done

# loop the resulting compose files
for i in "${compose_files[@]}"
do
    # retain qualified path to substitute for relative paths
    compose_path=${i/docker-compose.yaml/}
    
    # enumerate sops-targeted volumes within the compose file
    sops_volumes=()
    while IFS=  read ; do
        sops_volumes+=("$REPLY")
    done < <(yq eval '( .. | select(has("volumes")) | (.volumes[]) | select(.x-sops == "true") | (.source) )' $i)
    for x in "${sops_volumes[@]}"
    do
        # replace relative path with fully qualified    
        dec_fullpath=${x/.\//"$compose_path"}
        
        # add this file to the encryption queue
        files_to_encrypt+=("$dec_fullpath")
        
        # check .gitignore and insert this file if it is not present
        gitignore_fullpath="$compose_path.gitignore"
        # remove leading './'
        x=${x/.\//""}
        if [ -f $gitignore_fullpath ]; then
            # check for/insert relative path in .gitignore
            if ! grep -Fxq "$x" $gitignore_fullpath; then
                echo "adding $x to $gitignore_fullpath"
                echo "$x" >> $gitignore_fullpath
            fi
        else
            echo "adding $x to $gitignore_fullpath"
            echo "$x" >> $gitignore_fullpath
        fi
    done

    # enumerate secrets within the compose file
    sops_secrets=()
    while IFS=  read ; do
        sops_secrets+=("$REPLY")
    done < <(yq eval '( .. | select(has("secrets")) | (.secrets[]) | (.file) )' $i)
    for x in "${sops_secrets[@]}"
    do
        # replace relative path with fully qualified
        dec_fullpath=${x/.\//"$compose_path"}

        # add this file to the encryption queue
        files_to_encrypt+=("$dec_fullpath")

        # check .gitignore and insert this file if it is not present
        gitignore_fullpath="$compose_path.gitignore"
        # remove leading './'
        x=${x/.\//""}
        if [ -f $gitignore_fullpath ]; then
            # check for/insert relative path in .gitignore
            if ! grep -Fxq "$x" $gitignore_fullpath; then
                echo "adding $x to $gitignore_fullpath"
                echo "$x" >> $gitignore_fullpath
            fi
        else
            echo "adding $x to $gitignore_fullpath"
            echo "# generated automatically, based on sensitive files defined in docker-compose.yaml" >> $gitignore_fullpath
            echo "$x" >> $gitignore_fullpath
        fi
    done
done

# Main secrets operations loop
for dec_fullpath in "${files_to_encrypt[@]}"
do
    dec_extension="${dec_fullpath##*.}"
    enc_fullpath=${dec_fullpath/"$dec_extension"/"sops.$dec_extension"}

    if [ ! -f $dec_fullpath ]; then
        # decrypted file does not exist locally
        if [ -f $enc_fullpath ]; then
            if [ $rotate_keys ]; then
                echo "Decrypting: $dec_fullpath <- $enc_fullpath"
                sops -d $enc_fullpath > $dec_fullpath
                echo "Encrypting: $dec_fullpath -> $enc_fullpath"
                sops -e $dec_fullpath > $enc_fullpath
            fi
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
                    echo
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
                echo
                if [[ $REPLY =~ ^[Yy]$ ]]; then
                    rm $dec_fullpath
                fi
            fi
        fi
    fi

done