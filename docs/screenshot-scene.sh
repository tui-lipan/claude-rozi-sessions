# Scene for the extension gallery screenshot; see the rozi-extension skill's screenshots guide.
# One Claude Code client with background conversations in two repositories and a worktree. `claude`
# is a stand-in: `claude agents --json` lists staged sessions, and run plainly it draws an idle
# Claude screen, so no real Claude Code or account is involved.
repo() {
    git init -q -b "$2" "$1"
    git -C "$1" -c user.name=demo -c user.email=demo@example.invalid commit -q --allow-empty -m init
}
repo "$HOME/src/rozi" master
git -C "$HOME/src/rozi" worktree add -q -b fix-login "$HOME/src/rozi-fix-login"
repo "$HOME/src/tui-lipan" main

cat > "$LAB/agents.json" <<JSON
[
  {"pid": PID, "id": "a1f0c2d4", "sessionId": "a1f0c2d4-0000-4000-8000-000000000001",
   "cwd": "$HOME/src/rozi", "kind": "interactive", "status": "busy",
   "name": "worktree unlock", "startedAt": 1},
  {"pid": 4242, "id": "b2e1d3c5", "sessionId": "b2e1d3c5-0000-4000-8000-000000000002",
   "cwd": "$HOME/src/rozi-fix-login", "kind": "background", "status": "waiting",
   "state": "working", "waitingFor": "approve Bash(npm test)", "name": "fix login redirect",
   "startedAt": 2},
  {"pid": 4243, "id": "c3f2e4d6", "sessionId": "c3f2e4d6-0000-4000-8000-000000000003",
   "cwd": "$HOME/src/tui-lipan", "kind": "background", "status": "idle", "state": "done",
   "name": "focused_node_id docs", "startedAt": 3},
  {"pid": 4244, "id": "d4a3f5e7", "sessionId": "d4a3f5e7-0000-4000-8000-000000000004",
   "cwd": "$HOME/src/tui-lipan", "kind": "background", "status": "busy", "state": "working",
   "name": "scroll anchor tests", "startedAt": 4}
]
JSON
bin claude "LAB='$LAB'"'
case "$1" in
    agents) sed "s/PID/$(cat "$LAB/claude.pid")/" "$LAB/agents.json"; exit ;;
    plugin) echo "[]"; exit ;;
esac
echo $$ > "$LAB/claude.pid"
rule=$(printf "─%.0s" $(seq 1 "$(stty size | cut -d" " -f2)"))
printf "\033]0;✳ worktree unlock\007\033[2J\033[H"
cat <<EOF

 ▐▛███▜▌   Claude Code
▝▜█████▛▘  ~/src/rozi
  ▘▘ ▝▝

❯ unlock a worktree that git reports as locked

● An interrupted \`git worktree add\` left a stale lock.
  I removed it and added a test that unlocks it again.

  ⎿  src/worktrees.rs  +18 -2
     tests/worktrees.rs  +41

✻ Sautéed for 1m 12s
$rule
❯
$rule
  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
EOF
while read -r _; do :; done'

# Lay out first: the stand-in draws its screen at the pane's width when it starts.
keys "cd ~/src/rozi" Enter
rozi split --cwd "$HOME/src/rozi-fix-login" --argv bash >/dev/null
# The sidebar: `Ctrl+A`, then `b`.
keys C-a b
settle 1000
rozi focus 1 >/dev/null
settle 300
keys "clear; claude" Enter
# The service polls every two seconds.
settle 5000
