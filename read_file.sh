#!/usr/bin/env bash


dir = $1
echo $dir

for file in $(ls $dir); do
    echo $file
done
