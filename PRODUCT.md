# Worker Desk

## Platform
Authenticated responsive web application; desktop and iPad are primary surfaces.

## Users and purpose
Developers operating a local development host through Codex, Claude Code, and OpenCode. The home page makes host health, active work, and remaining execution resources understandable at a glance. Existing task dispatch, history, memory, and model management remain dedicated views.

## Confirmed product decisions
The user approved equal prominence for host performance and ongoing work, with structured remote controls: thermal presets, fan speed, supported system power modes, keep-awake, processes, managed services, sleep, restart, and shutdown. No browser terminal or file manager in this release. Controls report observed state; accepted commands are not proof of completion. Disruptive operations expose affected work and require confirmation. Do not interrupt active work during deployment verification.

## Interface
Extend the existing restrained Worker Desk visual language. Current numbers and meaningful curves take priority over descriptive paragraphs. Details and controls open progressively. Preserve bilingual Chinese/English support and existing locale fallback. Missing telemetry is unavailable, not zero; missing progress never becomes a fabricated percentage. Respect reduced motion.

## Stack and integration
Existing Python console and vanilla HTML/CSS/JavaScript. Shared telemetry serves web and physical information board; its absence must not block web monitoring. Optional platform capabilities must not block basic operation. Credentials never enter telemetry, history, source, or logs.

## Success
A user can identify host load, current jobs, resource pressure, and service faults immediately, then make an intentional control change and see whether it took effect. Existing task routes and workload execution remain compatible.
