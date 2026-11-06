#!/bin/sh
# 在三个固定分支之间切换：
#   main      最稳定，只合并 dev 验证过的内容
#   dev       相对稳定，日常开发的集成分支
#   dev-next  最不稳定，dev 的先进分支，新功能先在这里试，稳定后再并入 dev
# 用法：sh switch.sh            列出三个分支，输入编号切换
#       sh switch.sh <分支>     直接切到 main / dev / dev-next
# 有未提交的改动时先选：提交到当前分支，或暂存（以后切回这个分支时自动恢复）。
# 中文提示里的变量一律写成 ${var}：bash 3.2 在 UTF-8 下会把紧跟的中文字节当成变量名的一部分。
set -eu
B1=main
B2=dev
B3=dev-next
MARK="[switch.sh]" # 本脚本暂存时写进说明的标记，切回时按它找回
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

current=$(git symbolic-ref --short -q HEAD) || die "当前不在任何分支上（detached HEAD），先手动 git switch 到 ${B1}、${B2} 或 ${B3}。"

if [ -n "${1:-}" ]; then
  target=$1
else
  echo "当前在 ${current}。可切换的分支："
  for pair in "1 ${B1} 最稳定" "2 ${B2} 相对稳定" "3 ${B3} 最不稳定（dev 的先进分支）"; do
    set -- $pair
    num=$1
    name=$2
    shift 2
    mark=" "
    [ "$name" = "$current" ] && mark="*"
    printf ' %s %s) %s  %s\n' "$mark" "$num" "$name" "$*"
  done
  printf '输入编号或分支名（回车取消）：'
  read -r choice || :
  case "${choice:-}" in
    1 | "$B1") target=$B1 ;;
    2 | "$B2") target=$B2 ;;
    3 | "$B3") target=$B3 ;;
    "") echo "已取消，没有切换。"; exit 0 ;;
    *) die "无效的选择：${choice}" ;;
  esac
fi

case "$target" in
  "$B1" | "$B2" | "$B3") ;;
  *) die "只能在 ${B1}、${B2}、${B3} 之间切换，收到：${target}" ;;
esac
if [ "$target" = "$current" ]; then
  echo "已经在 ${target} 上。"
  exit 0
fi
git rev-parse -q --verify "refs/heads/$target" >/dev/null ||
  git rev-parse -q --verify "refs/remotes/origin/$target" >/dev/null ||
  die "找不到分支 ${target}。"
git_dir=$(git rev-parse --git-dir)
for f in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
  if [ -e "$git_dir/$f" ]; then
    die "正处在合并/变基/拣选的中途，先处理完（git status 查看）再切换。"
  fi
done

if [ -n "$(git status --porcelain)" ]; then
  echo "${current} 上有未提交的改动："
  git status --short
  printf '[c] 提交到 %s  [s] 暂存，切回 %s 时自动恢复  [q] 取消：' "$current" "$current"
  read -r choice || :
  case "${choice:-}" in
    c | C)
      default="wip: 切到 ${target} 前提交未完成的改动"
      printf '提交说明（回车用“%s”）：' "$default"
      read -r msg || :
      git add -A
      git commit -m "${msg:-$default}"
      ;;
    s | S)
      git stash push -u -m "${MARK} 切到 ${target} 前自动暂存"
      ;;
    *)
      echo "已取消，没有切换。"
      exit 0
      ;;
  esac
fi

git switch "$target"

# 找本脚本上次在 target 上暂存的改动（取最新一条），有就恢复。
# 模式前的 "(" 不能省：bash 3.2 在 $() 里解析 case 有 bug。
stash=$(git stash list --format='%gd %gs' | while read -r ref subject; do
  case "$subject" in ("On $target: $MARK"*)
    echo "$ref"
    break
    ;;
  esac
done)
if [ -n "$stash" ]; then
  echo "恢复 ${target} 上次切走时暂存的改动（${stash}）："
  git stash pop --index "$stash" ||
    die "没能自动恢复，暂存仍保留在 ${stash}（没有删除）。按上面 git 的提示处理，确认改动都回来了再 git stash drop ${stash}。"
fi
