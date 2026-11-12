# Bash completion for lang-switcher. Read-only: no CLI, D-Bus or network calls.

_lang_switcher_complete_dirs()
{
    local search=$1 candidate
    case "$search" in
        '~') search=$HOME ;;
        '~/'*) search="$HOME/${search:2}" ;;
    esac
    while IFS= read -r candidate; do
        COMPREPLY+=("$candidate")
    done < <(compgen -d -- "$search")
    compopt -o filenames 2>/dev/null || :
}

_lang_switcher_complete_backups()
{
    local cur=$1 root=$2 candidate
    if [[ $cur != */* && $cur != '~'* ]]; then
        for candidate in "$root"/*; do
            [[ -d $candidate && -f $candidate/state.json ]] || continue
            [[ ${candidate##*/} == "$cur"* ]] && COMPREPLY+=("$candidate")
        done
    fi
    if ((${#COMPREPLY[@]})); then
        compopt -o filenames 2>/dev/null || :
    else
        _lang_switcher_complete_dirs "$cur"
    fi
}

_lang_switcher()
{
    local cur=${COMP_WORDS[COMP_CWORD]-} prev='' action=${COMP_WORDS[1]-}
    local option word used
    local -a options=()
    COMPREPLY=()
    compopt +o filenames 2>/dev/null || :
    ((COMP_CWORD > 0)) && prev=${COMP_WORDS[COMP_CWORD-1]}

    if ((COMP_CWORD == 1)); then
        while IFS= read -r word; do COMPREPLY+=("$word"); done < <(
            compgen -W 'status update rollback uninstall restore completion layout case help --help -h' -- "$cur"
        )
        return 0
    fi

    if [[ $action == completion ]]; then
        if ((COMP_CWORD == 2)); then
            while IFS= read -r word; do COMPREPLY+=("$word"); done < <(
                compgen -W 'bash --help -h' -- "$cur"
            )
        fi
        return 0
    fi

    if [[ $action == update ]]; then
        if [[ $prev == --repository ]] ||
           [[ $prev == = && ${COMP_WORDS[COMP_CWORD-2]-} == --repository ]]; then
            while IFS= read -r word; do COMPREPLY+=("$word"); done < <(
                compgen -W 'AlexZhilov/lang-switcher' -- "$cur"
            )
            return 0
        fi
        if [[ $cur == --repository=* ]]; then
            [[ AlexZhilov/lang-switcher == "${cur#--repository=}"* ]] &&
                COMPREPLY=('--repository=AlexZhilov/lang-switcher')
            return 0
        fi
    fi

    if [[ $action == restore ]] && ((COMP_CWORD == 2)); then
        _lang_switcher_complete_backups "$cur" "$HOME/.local/share/lang-switcher/backups"
        return 0
    fi

    case "$action" in
        status|rollback|uninstall|restore) options=(--verbose --help -h) ;;
        update) options=(--verbose --repository --help -h) ;;
        layout|case|help|--help|-h) options=(--help -h) ;;
        *) return 0 ;;
    esac
    for option in "${options[@]}"; do
        [[ $option == "$cur"* ]] || continue
        used=false
        for word in "${COMP_WORDS[@]:2:COMP_CWORD-2}"; do
            [[ $word == "$option" || $word == "$option"=* ]] && used=true
        done
        [[ $used == true ]] || COMPREPLY+=("$option")
    done
    return 0
}

complete -F _lang_switcher lang-switcher
