claude() {
  if [[ "$PWD" == "$HOME/.claude" ]]; then
    clear && command claude "/color orange" "$@"
  else
    clear && command claude "$@"
  fi
}
