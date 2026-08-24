#!/usr/bin/env bash
# Hook UserPromptSubmit — recuerda el protocolo de gestión de tareas en cada prompt.
#
# Lo ejecuta el harness, no el modelo: por eso el recordatorio no se puede "olvidar"
# ni diluir cuando el contexto se comprime en una sesión larga. Lo que salga por stdout
# se inyecta en el contexto del turno.
#
# Protocolo completo: skill `clickup-task-flow`. Detalle de las tareas: TODO.md.
#
# REGLA DE ORO: este hook NUNCA debe fallar ni bloquear el prompt. Sin `set -e`, y
# `exit 0` incondicional al final. Tampoco hace llamadas de red: corre en cada turno.

set -uo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
CLAIM="$PROJECT_DIR/.claude/.tarea-actual"

if [ -f "$CLAIM" ]; then
    # Una sola línea, acotada: el archivo es local y editable a mano.
    EN_CURSO="$(tr -d '\r\n' < "$CLAIM" 2>/dev/null | cut -c1-200)"
    if [ -n "$EN_CURSO" ]; then
        printf '[protocolo de tareas] TAREA EN CURSO: %s\nAl terminar cerrala con `/tarea fin <P-XX>` (pone `complete` en ClickUp + comentario FIN y mueve el ítem a Realizadas en TODO.md). Si la abandonás a mitad, va a `on hold` o `update required`, nunca se deja en `in progress`.\n' "$EN_CURSO"
        exit 0
    fi
fi

printf '%s\n' '[protocolo de tareas] Ninguna tarea reclamada en este repo. Si lo que sigue es implementar, arreglar, verificar o refactorizar algo, primero invocá la skill `clickup-task-flow` (o `/tarea P-XX`) para validar en ClickUp que nadie más la esté haciendo. Antes de sacar trabajo nuevo del backlog, mirá `/tarea bloqueos`: son las tareas que el frontend devolvió en `on hold` porque el backend no cumplió el handoff, no salen en ningún otro filtro, y del otro lado hay una implementación parada. Responder preguntas, leer código o explicar cosas NO requiere reclamar tarea.'
exit 0
