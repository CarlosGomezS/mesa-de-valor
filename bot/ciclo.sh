#!/usr/bin/env bash
# Relevo continuo: GitHub solo lanza las ejecuciones programadas cada 3-4 h (o menos), así que cada
# ejecución hace una pasada cada 10 min durante unas 5 h y media y, al terminar, lanza la siguiente.
# Si mientras tanto hay otra ejecución esperando (botón «Buscar ahora» de la web o la programada),
# esta termina enseguida y le deja el sitio, para que la web nunca tenga que esperar.
set -u
START=$(date +%s)
MAX_SECONDS=${MAX_SECONDS:-19800}     # 5 h 30 min (GitHub corta a las 6 h)
EVERY=${EVERY:-600}                   # una pasada cada 10 min
REPO="$GITHUB_REPOSITORY"
api() { curl -sS -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" "$@"; }

waiting_run() {   # ¿hay otra ejecución de este flujo esperando turno?
  for st in pending queued waiting; do
    n=$(api "https://api.github.com/repos/$REPO/actions/workflows/bot.yml/runs?status=$st&per_page=5" \
        | python3 -c "import sys,json;d=json.load(sys.stdin);print(sum(1 for r in d.get('workflow_runs',[]) if str(r['id'])!='$GITHUB_RUN_ID'))" 2>/dev/null || echo 0)
    [ "${n:-0}" != "0" ] && return 0
  done
  return 1
}

save_state() {
  git add docs/data/state.enc docs/data/diag.txt docs/data/settings.enc 2>/dev/null || git add docs/data/state.enc
  git diff --cached --quiet && return 0
  git commit -qm "estado"
  for i in 1 2 3; do git pull -q --rebase -X theirs && git push -q && return 0; sleep 5; done
}

save_lab() {
  [ -s lab.enc ] || return 0
  if [ -f "$RUNNER_TEMP/lab.last" ] && cmp -s lab.enc "$RUNNER_TEMP/lab.last"; then return 0; fi
  rm -rf "$RUNNER_TEMP/lab" && mkdir "$RUNNER_TEMP/lab" && cp lab.enc "$RUNNER_TEMP/lab/"
  ( cd "$RUNNER_TEMP/lab" && git init -q -b lab-data && git add lab.enc \
    && git -c user.name=mesa-bot -c user.email=mesa-bot@users.noreply.github.com commit -qm "memoria del laboratorio" \
    && git push -q -f "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git" lab-data ) && cp lab.enc "$RUNNER_TEMP/lab.last"
}

git config user.name "mesa-bot"
git config user.email "mesa-bot@users.noreply.github.com"
if git fetch -q --depth=1 origin lab-data 2>/dev/null; then git show FETCH_HEAD:lab.enc > lab.enc; cp lab.enc "$RUNNER_TEMP/lab.last"; fi

pass=0
handover=0
while :; do
  pass=$((pass + 1))
  t0=$(date +%s)
  git pull -q --rebase -X theirs || true          # trae lo que hayas cambiado desde la web
  if [ "$pass" -gt 1 ]; then export FORZAR=; fi   # solo la primera pasada obedece a la web («Buscar ahora» / «Actualizar»)
  timeout 540 python bot/run.py || echo "La pasada $pass falló; sigo con la siguiente."
  save_state || echo "No pude guardar el estado en esta pasada."
  save_lab || echo "No pude guardar la memoria del laboratorio."
  # esperar a la siguiente pasada, cediendo el sitio si llega otra ejecución
  while [ $(( $(date +%s) - t0 )) -lt "$EVERY" ]; do
    if waiting_run; then handover=1; break 2; fi
    [ $(( $(date +%s) - START )) -ge "$MAX_SECONDS" ] && break 2
    sleep 30
  done
  [ $(( $(date +%s) - START )) -ge "$MAX_SECONDS" ] && break
done

if [ "$handover" = 1 ]; then
  echo "Otra ejecución espera turno: le dejo el relevo."
elif waiting_run; then
  echo "Ya hay una ejecución esperando: no lanzo otra."
else
  echo "Lanzo el relevo."
  api -X POST "https://api.github.com/repos/$REPO/actions/workflows/bot.yml/dispatches" \
      -d '{"ref":"main","inputs":{"forzar":"relevo"}}' || echo "No pude lanzar el relevo (la programada lo retomará)."
fi
