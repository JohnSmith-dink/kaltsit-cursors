#!/usr/bin/env sh
set -eu

usage() {
    printf '%s\n' 'Usage: sh install.sh [--prefix ICONS_DIRECTORY] [--uninstall]' \
        'Default: $XDG_DATA_HOME/icons, or $HOME/.local/share/icons' \
        'Installs files only. Select Kaltsit in your desktop cursor settings afterwards.'
}

prefix="${XDG_DATA_HOME:-$HOME/.local/share}/icons"
operation=install
while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; prefix=$2; shift 2 ;;
        --uninstall) operation=uninstall; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
case "$prefix" in /*) ;; *) prefix="$PWD/$prefix" ;; esac
destination="${prefix%/}/Kaltsit"
if [ -L "$destination" ]; then
    printf 'Refusing to replace a symlink: %s\n' "$destination" >&2
    exit 1
fi
if [ "$operation" = uninstall ]; then
    [ -d "$destination" ] && [ -f "$destination/.kaltsit-generated" ] || {
        printf 'No managed Kaltsit installation found: %s\n' "$destination" >&2; exit 1;
    }
    rm -rf -- "$destination"
    printf 'Removed %s\n' "$destination"
    exit 0
fi
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
source_dir="$script_dir/theme/Kaltsit"
[ -f "$source_dir/index.theme" ] && [ -d "$source_dir/cursors" ] || {
    printf '%s\n' 'Theme missing. Run python3 tools/build_theme.py first.' >&2; exit 1;
}
mkdir -p -- "$prefix"
stage=$(mktemp -d "$prefix/.kaltsit-install.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT HUP INT TERM
cp -R -- "$source_dir" "$stage/Kaltsit"
if [ -e "$destination" ]; then
    backup=$(mktemp -d "$prefix/Kaltsit.backup.XXXXXX")
    mv -- "$destination" "$backup/Kaltsit"
    printf 'Previous theme saved at %s/Kaltsit\n' "$backup"
fi
mv -- "$stage/Kaltsit" "$destination"
printf 'Installed %s\nSelect the Kaltsit cursor theme in your desktop settings.\n' "$destination"
