# Audit map

This file maps each claim in [SECURITY.md](SECURITY.md) to the code that keeps it and the test that shows it.
Each claim names a function or method in a part under `hoplon_core/`, and a test under `tests/`. Where no test
truly shows a claim, the row says `none`, and the last section lists those rows.

To audit hoplon, read `hoplon_core/gate.py` first: it alone decides whether anything happens. Then read the
handlers it calls: `files.py`, `commands.py` and `sandbox.py`. Then read `docs/json-protocol.md`, for what a
wrapper may send. `tests/test_parts.py` holds the structure. The gate touches nothing, only the places it lists
touch the system, and each part uses only the parts before it.

## 1. Prompt injection and goal hijack

| Claim | Kept by | Shown by |
| --- | --- | --- |
| The persona comes after the rules | `turn.py: Engine.system` | `test_hoplon.TestPersonaProfiles.test_profile_picks_endpoint_persona_skills_params` |
| A tool hoplon did not offer is invalid | `gate.py: Gate.make_proposal` | `test_invariants.TestTable.test_a_tool_not_in_the_table_is_invalid` |
| Shared text can't close its own fence | `base.py: shared_block` | `test_hoplon.TestSharedBlock.test_the_body_cant_close_its_own_fence` |
| Each share is logged by size and sha256 | `gate.py: Gate.share` | none |

## 2. Data exfiltration and the "lethal trifecta"

| Claim | Kept by | Shown by |
| --- | --- | --- |
| Redirects are refused | `client.py: NoRedirect.redirect_request` | `test_hoplon.TestEndToEnd.test_redirect_not_followed` |
| Proxy variables in the environment are ignored | `client.py: Client.__init__` | `test_hoplon.TestEndToEnd.test_env_proxy_ignored` |
| A model switch stays on the same server | `turn.py: Engine.set_model` | `test_hoplon.TestEndToEnd.test_models_listed_and_switched_on_the_same_server` |
| Odd model names are left out, the list is capped | `client.py: Client.model_list`<br>`client.py: Client.models` | none |
| A sandboxed run with the network on asks each time | `gate.py: Gate.standing` | `test_sandbox_runner.TestSandboxLoop.test_network_switch_makes_each_run_ask` |
| In coding mode a hidden file still asks | `gate.py: Gate.read_allowed` | `test_hoplon.TestEndToEnd.test_coding_mode_auto_reads_but_hidden_asks` |
| A file that looks secret is not read automatically | `gate.py: Gate.read_now` | `test_coding_guard.TestSecrets.test_secret_looking_file_needs_approval` |
| "No secrets" is kept by sha256 and lapses on change | `gate.py: Gate.secret_text`<br>`config.py: Config.set_not_secret` | `test_hoplon.TestEndToEnd.test_a_file_you_say_holds_no_secrets_reads_without_asking_until_it_changes` |
| A file holding the endpoint key is never excused | `gate.py: Gate.secret_text` | `test_hoplon.TestEndToEnd.test_the_endpoint_key_is_never_excused` |
| You mark a file yourself only as you saw it, never a secret name | `gate.py: Gate.mark_not_secret` | `test_hoplon.TestEndToEnd.test_you_can_mark_a_file_yourself_once_you_have_seen_it` |
| A run names the files your list let through | `sandbox.py: Sandbox.secret_file`<br>`gate.py: Gate.run_sandboxed` | `test_sandbox_runner.TestSandboxLoop.test_a_run_names_the_files_your_list_let_through` |
| An automatic search skips secret-named files | `files.py: Workdir.walk`<br>`gate.py: Gate.search` | `test_hoplon.TestEndToEnd.test_search_files_in_coding_mode` |

## 3. Excessive agency and tool misuse

| Claim | Kept by | Shown by |
| --- | --- | --- |
| `run_in_sandbox` is offered only in coding mode with a sandbox | `turn.py: Engine.tools`<br>`gate.py: Gate.can_run` | `test_sandbox_runner.TestSandboxLoop.test_not_offered_outside_coding_mode` |
| `search_files` is offered only in coding mode | `gate.py: Gate.can_search` | `test_hoplon.TestEndToEnd.test_search_files_is_not_offered_outside_coding_mode` |
| `..` and absolute paths are refused | `files.py: split_rel` | `test_hoplon.TestWorkdir.test_rejects_dotdot_and_absolute` |
| A symlinked directory in a path is refused | `files.py: Workdir._open_dir` | `test_hoplon.TestWorkdir.test_rejects_symlinked_directory_component` |
| Reads refuse FIFOs and binary files, and are capped | `files.py: Workdir.read` | `test_hoplon.TestWorkdir.test_binary_fifo_and_caps` |
| New files get no execute bit | `files.py: Workdir._create_new` | none |
| A new file never replaces one made meanwhile | `files.py: Workdir._publish` | `test_hoplon.TestWorkdir.test_new_file_publication_refuses_a_concurrent_creator` |
| A proposed command ends the turn | `turn.py: Engine.loop` | `test_hoplon.TestEndToEnd.test_command_proposal_ends_turn` |
| A turn pauses after `max_steps` steps nobody decided | `turn.py: Engine.loop` | `test_steps.TestSteps.test_unattended_steps_pause_at_max_steps` |
| Without a terminal or JSON mode, nothing is performed | `front.py: PlainUI.decide` | `test_hoplon.TestEndToEnd.test_plain_mode_performs_nothing` |

### Scoped commands (command sets)

| Claim | Kept by | Shown by |
| --- | --- | --- |
| A changed command set is refused | `commands.py: CommandSet.__init__`<br>`commands.py: command_set_status` | `test_commands.TestBlessing.test_blessed_is_enabled_and_changes_are_refused` |
| A value fills one argument; no shell | `commands.py: TrustedCommand.build`<br>`commands.py: run_program` | `test_commands.TestRunning.test_a_loose_value_stays_one_argument` |
| Values that break the rules are refused, not cleaned | `commands.py: TrustedParam.check` | `test_commands.TestRunning.test_awkward_values_are_refused_and_nothing_runs` |
| A path is resolved, then checked | `commands.py: TrustedParam.check_path` | `test_commands.TestManifest.test_a_path_is_resolved_then_checked` |
| No value may start with `-` | `commands.py: TrustedParam.check`<br>`commands.py: TrustedParam.__init__` | `test_commands.TestManifest.test_option_like_values_are_refused_whatever_the_pattern` |
| The program is found in `search`, never `PATH` | `commands.py: trusted_binary` | `test_commands.TestBinary.test_resolved_from_search_never_from_path` |
| A program replaced after loading is refused | `commands.py: TrustedCommand.changed`<br>`gate.py: Gate.run_trusted` | `test_commands.TestRunning.test_a_program_that_changed_is_refused` |
| The environment is minimal | `commands.py: TrustedCommand.env` | `test_commands.TestBinary.test_the_environment_is_minimal` |
| `PATH` and `LD_*` can't be listed; `access` is required | `commands.py: CommandSet.parse`<br>`commands.py: TrustedCommand.__init__` | `test_commands.TestManifest.test_limits_search_env_and_pane` |
| A run past its time limit is killed | `commands.py: run_program` | `test_commands.TestRunning.test_timeout_kills_the_program` |
| A write asks every time | `gate.py: Gate.standing` | `test_commands.TestRunning.test_not_read_only_asks_every_time` |
| A command that uses the network asks, reads too | `commands.py: TrustedCommand.plain` | `test_commands.TestBlessing.test_a_command_that_uses_the_network_asks_even_to_read` |
| A command run as root asks, reads too, and is listed apart | `commands.py: TrustedCommand.plain`<br>`commands.py: CommandSet.summary` | `test_commands.TestBlessing.test_a_command_run_as_root_is_listed_apart_and_asks_even_to_read` |
| A run you ask for won't run a root command that asks | `gate.py: Gate.run_user` | none |
| Output is masked for the endpoint key | `gate.py: Gate.run_trusted` | `test_commands.TestRunning.test_the_endpoint_key_is_masked` |

## 4. Identity, privilege and credentials

| Claim | Kept by | Shown by |
| --- | --- | --- |
| `key_env` must be a plain identifier | `client.py: Endpoint.__init__` | `test_snapshots.TestKeyEnvName.test_hoplon_rejects_non_identifier_key_env` |
| `key_file` must be 0600 and yours | `client.py: Endpoint.key` | `test_hoplon.TestEndpoints.test_key_file_mode` |
| `Authorization` is refused in `headers` | `client.py: Endpoint.__init__` | `test_hoplon.TestEndpoints.test_strict_fields` |
| Credentials in `base_url` are refused | `client.py: Endpoint.__init__` | none |
| The key is masked under any variable name | `gate.py: Gate.secrets`<br>`base.py: redact` | `test_masking.TestEndpointKey.test_masked_under_an_unknown_variable_name` |
| The key is masked in wrapper attachments | `front.py: JsonUI.queue_attach` | `test_masking.TestEndpointKey.test_json_attach` |

## 5. Unexpected code execution and sandbox escape

| Claim | Kept by | Shown by |
| --- | --- | --- |
| `:run` says where it runs | `front.py: Repl.do_run`<br>`base.py: where_runs` | `test_hoplon.TestEndToEnd.test_run_reports_outside_sandbox` |
| No sandboxed run where coding mode is refused | `sandbox.py: Sandbox.problem` | `test_sandbox_runner.TestSandboxConfig.test_refused_where_coding_mode_is_refused_even_with_coding_anywhere` |
| With no image, only host `/usr` and `/etc`, read-only | `sandbox.py: Sandbox.argv` | `test_sandbox_runner.TestArgv.test_no_image_runs_on_this_hosts_os` |
| No network, capabilities dropped, read-only root | `sandbox.py: Sandbox.argv` | `test_sandbox_runner.TestArgv.test_offline` |
| Secret names and contents are covered | `sandbox.py: Sandbox.masks` | `test_sandbox_runner.TestMasks.test_secret_names_and_contents_are_hidden` |
| The guard refuses a run if a secret is still there | `sandbox.py: Sandbox.run` | `test_sandbox_runner.TestRun.test_guard_refuses_when_a_secret_is_still_there` |
| A run past its time limit is killed | `sandbox.py: Sandbox._podman` | `test_sandbox_runner.TestRun.test_time_limit_kills_it` |
| A sample approval is for exactly what was listed | `sandbox.py: Fixtures.approve` | `test_fixtures.TestStore.test_approval_is_for_exactly_what_was_listed` |
| A change to samples drops the approval | `sandbox.py: Fixtures.mount` | `test_fixtures.TestStore.test_a_change_after_approval_forgets_it_and_stops_the_run` |
| A repository with Git LFS files is refused | `sandbox.py: Copy.create` | `test_work.TestCopy.test_a_repository_with_git_lfs_files_is_refused` |
| The repository's own filter drivers never run | `sandbox.py: run_git` | `test_work.WorkTest.test_the_repositorys_own_filter_drivers_never_run_in_the_copy_but_global_ones_do` |
| No file tool writes under `.git` | `gate.py: Gate.no_git` | `test_work.WorkTest.test_git_is_never_written_and_hidden_files_ask` |
| Writes ask until the plan is approved | `gate.py: Gate.work_edit` | `test_work.WorkTest.test_writes_ask_until_the_plan_is_approved` |
| The network stays off during a session | `gate.py: Gate.set_network`<br>`gate.py: Gate.perform_finish` | `test_work.WorkTest.test_the_network_stays_off_during_a_session` |
| Coding mode goes back after the session | `turn.py: Engine.put_back_coding` | `test_work.WorkTest.test_coding_mode_goes_back_to_off_after_a_refusal_and_after_the_session` |

## 6. Improper output handling: the terminal as an attack surface

| Claim | Kept by | Shown by |
| --- | --- | --- |
| Controls and bidirectional overrides are escaped | `base.py: exact` | `test_hoplon.TestExactAndRedact.test_exact_flags_controls_and_bidi` |
| A sequence split across chunks is still escaped | `base.py: exact` | `test_display.TestEscapedModelText.test_escape_split_across_chunks` |
| A reason or path stays on one line | `base.py: one_line` | `test_escaping.TestProposalDisplay.test_reason_and_path_stay_on_one_line` |
| File contents carry a margin | `base.py: margin` | `test_escaping.TestProposalDisplay.test_body_lines_are_all_behind_the_margin` |
| Non-ASCII letters in a command are named | `base.py: non_ascii_line` | `test_escaping.TestProposalDisplay.test_non_ascii_command_is_listed` |
| The endpoint review screen is escaped | `client.py: Endpoint.summary` | `test_escaping.TestReviewScreens.test_endpoint_with_cursor_control_model` |
| In JSON mode a command with hidden text is flagged | `front.py: JsonUI.show_command` | `test_hoplon.TestEndToEnd.test_hidden_command_text_is_flagged` |

## 7. Approval integrity and human trust

| Claim | Kept by | Shown by |
| --- | --- | --- |
| End of input at the terminal declines | `base.py: ask_yes`<br>`front.py: TtyUI.decide` | `test_invariants.TestGate.test_end_of_input_at_the_terminal_declines_every_kind_that_asks` |
| End of input in JSON mode declines | `front.py: JsonUI.listen` | `test_hoplon.TestEndToEnd.test_eof_means_decline` |
| Pending proposals don't survive a resume | `turn.py: Session.close_dangling` | `test_hoplon.TestEndToEnd.test_session_resume_closes_pending` |
| A file changed after the preview is not written | `files.py: Workdir.write`<br>`gate.py: Gate.perform_file` | `test_approvals.TestStaleApprovals.test_write_to_file_modified_after_preview_is_refused` |
| A decision must echo the proposal's digest | `front.py: JsonUI.decide` | `test_hoplon.TestEndToEnd.test_a_decision_must_echo_the_proposals_digest` |
| A handler runs only after an accept | `gate.py: Gate.perform` | `test_invariants.TestNothingRunsUnaccepted.test_undecided_and_declined_never_reach_the_handler` |
| One function answers whether a rule approves | `gate.py: Gate.standing` | `test_invariants.TestTable.test_standing_is_the_one_place_a_rule_approves` |
| Only `grant` makes a proposal automatic | `gate.py: Gate.grant` | `test_invariants.TestTable.test_only_grant_makes_a_proposal_automatic` |
| A rule approves only the kinds that list it | `gate.py: Gate.grant` | `test_invariants.TestGate.test_a_rule_approves_only_the_kinds_that_list_it` |
| An automatic approval is logged with its rule | `gate.py: Gate.decide` | `test_invariants.TestGate.test_every_automatic_approval_is_audited_with_its_rule_before_it_is_accepted` |
| An edit that can't be previewed is not applied | `gate.py: Gate.build_edit` | none |
| An undiffable overwrite shows the content and mode | `front.py: TtyUI.decide` | `test_escaping.TestProposalDisplay.test_overwriting_a_binary_file_shows_content_and_mode` |
| New, hidden and secret files always ask | `gate.py: Gate.auto_edit` | `test_edits.TestApprovedFiles.test_secret_hidden_and_new_files_always_ask` |
| Coding mode is refused in home and credential directories | `sandbox.py: coding_refusal` | `test_coding_guard.TestWhere.test_refused_places` |
| A resumed session is checked again | `turn.py: Engine.__init__` | `test_coding_guard.TestWhere.test_resumed_session_is_checked` |
| A profile can't override the refusal | `turn.py: Engine.__init__` | `test_coding_guard.TestOverride.test_a_profile_cannot_override` |
| A planned command is preapproved once | `gate.py: Gate.match_plan` | `test_hoplon.TestEndToEnd.test_a_planned_command_is_preapproved_once` |

### Remote hoplon

| Claim | Kept by | Shown by |
| --- | --- | --- |
| `key_env`, `key_file`, `proxy` and other URLs are refused | `config.py: remote_config` | `test_remote_config.TestRemoteConfigLine.test_refusals` |
| The export carries no key, headers or proxy | `config.py: export_remote` | `test_remote_config.TestRemoteConfigExport.test_exports_without_key_headers_or_proxy` |
| Skills and command sets are built from the bytes sent | `config.py: remote_items` | `test_remote_config.TestRemoteConfigLine.test_items_are_made_from_the_bytes_sent` |
| A hash that differs refuses the whole config | `config.py: remote_items` | `test_remote.TestCommandSetsAndSkills.test_a_changed_file_in_the_config_line_is_refused` |
| A remote session is not resumed locally | `turn.py: Session.open` | `test_remote_config.TestRemoteConfigLine.test_a_remote_session_is_not_resumed_by_hoplon_here` |
| `hello` names the bundle that ran | `front.py: running_bundle` | `test_remote.TestBundle.test_hello_names_the_bundle_that_was_sent` |

## 8. Supply chain and configuration trust

| Claim | Kept by | Shown by |
| --- | --- | --- |
| Nothing is sent until the endpoint is blessed | `config.py: select_named` | `test_hoplon.TestEndpoints.test_selection_requires_blessing` |
| Blessing needs a terminal | `front.py: main` | none |
| An endpoint is hashed and parsed from one read | `config.py: load_named` | `test_snapshots.TestEndpointSingleRead.test_swap_between_hash_and_parse_cannot_happen` |
| A skill is hashed across its files, symlinks refused | `base.py: snapshot_skill` | `test_hoplon.TestSkills.test_hash_changes_and_symlinks` |
| A group-writable state parent is refused | `base.py: state_directory` | `test_hoplon.TestStatePermissions.test_group_writable_state_parent_is_refused` |
| Unknown fields and `params.model` are refused | `client.py: Endpoint.__init__` | `test_hoplon.TestEndpoints.test_strict_fields` |
| The notify hook is re-checked before each run | `config.py: hook_problem`<br>`config.py: Notifier.fire` | `test_hooks.TestNotifierChecks.test_edited_after_blessing` |
| The hook doesn't get the key variable | `config.py: Notifier.fire` | `test_hooks.TestNotifierRuns.test_key_variable_is_removed_and_others_are_kept` |
| The hook runs only for a person at a terminal | `front.py: main` | `test_hooks.TestCli.test_not_active_in_json_mode` |
| The gate itself touches nothing | `gate.py: Gate` | `test_parts.TestParts.test_the_gate_touches_nothing` |
| Only the handlers and listed places touch the system | `gate.py: GATE_REQUESTS` | `test_parts.TestParts.test_only_the_handlers_touch_the_system_and_every_other_place_is_listed` |

## 9. Memory and context poisoning

| Claim | Kept by | Shown by |
| --- | --- | --- |
| A session file is 0600 | `turn.py: Session.save`<br>`base.py: write_private` | `test_hoplon.TestEndToEnd.test_context_limit_refuses_and_compact_prunes` |
| Compaction keeps the full history in the file | `turn.py: Engine.compact` | `test_hoplon.TestEndToEnd.test_context_limit_refuses_and_compact_prunes` |
| A compaction summary is shown and can be edited | `turn.py: Engine.compact` | none |
| A session resumed elsewhere has coding mode off | `front.py: main` | none |

## 10. Sensitive information disclosure

| Claim | Kept by | Shown by |
| --- | --- | --- |
| Output is masked before the editor | `front.py: review_output` | `test_masking.Editor.test_key_and_patterns_masked_before_the_editor` |
| What you save is sent exactly | `front.py: review_output` | `test_display.TestRunShare.test_shared_message_is_exactly_the_saved_buffer` |
| A wrapper's `read` goes only to the wrapper | `gate.py: Gate.view` | `test_hoplon.TestEndToEnd.test_read_shows_a_file_to_the_user_only` |
| A failed edit on an unreadable file says only "declined" | `gate.py: Gate.build_edit` | `test_approvals.TestEditDisclosure.test_hidden_file_with_coding_off_gives_only_the_generic_result` |
| A path error says only "declined" where reads ask | `gate.py: Gate.make_proposal` | `test_coding_guard.TestDiskFacts.test_model_hears_only_declined_when_reads_need_approval` |
| The audit log keeps a command's hash, not its text | `base.py: command_fingerprint` | `test_display.TestRunShare.test_proposal_audit_has_hash_not_command` |
| The editor's temporary file is 0600 | `front.py: edit_text` | none |

## 11. Unbounded consumption

| Claim | Kept by | Shown by |
| --- | --- | --- |
| At the context limit nothing is sent | `turn.py: Engine.check_budget` | `test_hoplon.TestEndToEnd.test_context_limit_refuses_and_compact_prunes` |
| A whole reply is capped | `client.py: Client._chat` | `test_masking.TestReplyCap.test_oversized_non_streaming_reply` |
| A streamed reply is capped | `client.py: Client._stream` | `test_masking.TestReplyCap.test_oversized_streamed_reply` |
| Masking runs in linear time | `base.py: redact` | `test_masking.TestSpeed.test_redact_is_fast_on_adversarial_input` |

## 12. Logging and accountability

| Claim | Kept by | Shown by |
| --- | --- | --- |
| A `:run` is logged by hash, not text | `gate.py: Gate.run_shell` | `test_display.TestRunShare.test_run_audit_has_hash_not_command` |
| A blessing is logged | `config.py: bless` | none |
| Every file the model reads by rule is logged | `gate.py: Gate.decide` | `test_invariants.TestGate.test_a_real_call_of_each_automatic_file_kind_is_audited` |

## 13. Transport

| Claim | Kept by | Shown by |
| --- | --- | --- |
| HTTPS is needed except for loopback or `allow_http` | `client.py: Endpoint.__init__` | `test_hoplon.TestEndpoints.test_https_rules` |
| Blessing flags `allow_http` | `client.py: Endpoint.summary` | none |
| Certificates are checked, with `ca_bundle` if named | `client.py: Client.__init__` | none |

## Findings from the 2026-11-21 review

| Claim | Kept by | Shown by |
| --- | --- | --- |
| 2: an undiffable overwrite shows the new content | `front.py: TtyUI.decide` | `test_escaping.TestProposalDisplay.test_non_executable_binary_has_no_executable_note` |
| 7: coding mode doesn't auto-read a secret file | `gate.py: Gate.read_now` | `test_coding_guard.TestOverride.test_secret_looking_file_still_asks` |
| 8: hoplon masks its own key | `gate.py: Gate.secrets` | `test_masking.TestEndpointKey.test_engine_secrets_with_and_without_a_key` |

## Findings from the 2026-11-27 audit (OWASP AISVS 1.0, chapter C9)

| Claim | Kept by | Shown by |
| --- | --- | --- |
| 2: an edit in hoplon's own config or state asks | `gate.py: Gate.own_place` | `test_edits.TestOwnPlaces.test_own_places_always_ask` |

## Claims with no test

- 1: Each share is logged by size and sha256
- 2: Odd model names are left out, the list is capped
- 3: New files get no execute bit
- 3: A run you ask for won't run a root command that asks
- 4: Credentials in `base_url` are refused
- 7: An edit that can't be previewed is not applied
- 8: Blessing needs a terminal
- 9: A compaction summary is shown and can be edited
- 9: A session resumed elsewhere has coding mode off
- 10: The editor's temporary file is 0600
- 12: A blessing is logged
- 13: Blessing flags `allow_http`
- 13: Certificates are checked, with `ca_bundle` if named
