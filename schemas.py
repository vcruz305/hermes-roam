"""Tool schemas — what the LLM sees."""

ROAM_STATUS = {
    "name": "roam_status",
    "description": (
        "Report the sync status of the Hermes home: the local device, the "
        "configured backend, the last generation this device pushed and "
        "pulled, the remote head generation, whether the remote is ahead of "
        "this device, whether local shared files changed since the last sync, "
        "and the fleet's device roster. Read-only; use it before and after "
        "roam_push and roam_pull, and whenever the user asks whether their "
        "Hermes setup is in sync across machines."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

ROAM_PUSH = {
    "name": "roam_push",
    "description": (
        "Build an encrypted bundle of the shared Hermes home files (skills, "
        "memories, SOUL.md, config.yaml minus host-specific keys; secrets are "
        "never included) and upload it as a new generation. Refuses with a "
        "conflict error when another device pushed first; force=true overrides "
        "that check, dry_run=true reports what would be sent without uploading."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "force": {
                "type": "boolean",
                "description": (
                    "Push even though the remote head moved past what this "
                    "device last saw. Only use this when the user explicitly "
                    "accepts overwriting the fleet's newest generation."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "List the bundle's file paths and the total size without "
                    "uploading anything."
                ),
            },
        },
        "required": [],
    },
}

ROAM_PULL = {
    "name": "roam_pull",
    "description": (
        "Download and verify the newest remote generation of the shared Hermes "
        "home. DESTRUCTIVE: apply=true rewrites files in the Hermes home after "
        "making a backup (the previous files are copied to a backup directory "
        "and the install is rolled back on failure). With the default "
        "apply=false nothing is written; the tool only returns the plan of "
        "what would change, and the plan is complete: it lists the config "
        "files that would be merged (host-scoped keys stay local), config "
        "files that changed both locally and remotely (kept local unless "
        "the user opts into overwrite), the files whose content is already "
        "identical to the remote copy (unchanged — never rewritten, no "
        "backup taken), and every added, changed or removed path. Report the "
        "plan counts to the user exactly as given; never promise that the "
        "plan covers more or less than it does. apply=true is honoured only "
        "when the user enabled the plugin config key allow_agent_apply; with "
        "it off (the default) the tool refuses apply=true and you must ask "
        "the user to run `/roam pull apply` or `hermes roam pull --apply` "
        "themselves. Host-specific config keys stay local either way, and "
        "secrets are never synced."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "apply": {
                "type": "boolean",
                "description": (
                    "Install the downloaded files. apply=true rewrites files "
                    "in the Hermes home after making a backup; keep it false "
                    "to only review the plan. Refused unless the user enabled "
                    "config key allow_agent_apply — then ask the user to run "
                    "`/roam pull apply` or `hermes roam pull --apply`."
                ),
            },
            "classes": {
                "type": "array",
                "items": {"type": "string", "enum": ["shared", "sessions", "cron"]},
                "description": (
                    "Scope classes to include: shared (default), sessions, "
                    "cron. Sessions and cron are opt-in and usually stay local."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "Verify the remote bundle is readable without computing "
                    "the full install plan."
                ),
            },
        },
        "required": [],
    },
}
