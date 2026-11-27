# Help the user write a hoplon endpoint

You are helping the user create an endpoint for hoplon, the command-line client they are using right now. An endpoint tells hoplon where prompts go and which model answers. Treat it as a security-relevant file: it decides where the user's prompts, files and API key are sent. (Personas, default skills and coding mode belong in a *profile*, which names an endpoint; the `hoplon-profile` skill helps with those.)

## What an endpoint is

One JSON file. hoplon has no default endpoint and ignores `OPENAI_BASE_URL` and proxy variables, so nothing is sent until an endpoint exists and has been blessed.

```json
{
  "name": "hpc-llm",
  "description": "Optional one-line description",
  "base_url": "YOUR-SERVER-ADDRESS/v1",
  "model": "enterprise-large",
  "key_env": "HPC_LLM_KEY",
  "context_limit": 120000,
  "timeout": 120,
  "ca_bundle": "/etc/pki/internal-ca.pem",
  "headers": {},
  "params": {"temperature": 0.2}
}
```

Fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Used with `hoplon -e NAME` and in a profile's `"endpoint"`. Letters, digits, `.`, `_`, `-`. Match the file name (`NAME.json`). |
| `base_url` | yes | The OpenAI-compatible base URL, usually ending in `/v1`. hoplon calls `<base_url>/chat/completions`. |
| `model` | yes | The model id the endpoint expects, or one of its `aliases`. |
| `aliases` | no | Names of the user's own for the server's models, such as `{"low": "flash-model", "high": "pro-model"}`. A profile, a work session's planner and `:model` can then say `high`. Suggest the same names on every endpoint (`low`, `medium`, `high`), so profiles work with any of them. Ask which model should be each name; propose `curl <base_url>/models` for the user to run if they don't know the ids. |
| `key_env` | one of | Name of an environment variable holding the key. |
| `key_file` | one of | Path to a file holding the key. The file must be mode 0600 and owned by the user. |
| `context_limit` | no | The model's context window in tokens (default 32000). hoplon warns at 80% and refuses to send at 100%. |
| `timeout` | no | Seconds per request (default 120). Raise it for slow local models. |
| `ca_bundle` | no | PEM file for an internal certificate authority. |
| `headers` | no | Extra HTTP headers. Never credentials: `Authorization` and `Cookie` are refused. |
| `params` | no | Extra request fields such as `temperature` or `max_tokens`. `"stream": false` turns streaming off. May not set `model`, `messages` or `tools`. |
| `allow_http` | no | Needed for plain `http://` to anything other than localhost. Avoid it. |
| `proxy` | no | An explicit `http(s)://` proxy. Environment proxy variables are ignored. |

Unknown fields are an error, so don't invent any.

## Rules to follow

- **Never put the key in the endpoint file**, and never ask the user to paste their key to you. An endpoint only says where the key comes from (`key_env` or `key_file`). There is no option to run a command to fetch the key.
- **HTTPS only**, except for loopback addresses. If the user's endpoint is plain HTTP on another host, say plainly that prompts, files and the key would travel unencrypted, and suggest HTTPS first.
- Redirects are not followed. If the endpoint redirects, the user needs the final URL.

## How to work

1. Ask for what you don't know, one question at a time: the base URL, the model id, how the user keeps their key (an environment variable or a file), and the context window if they know it.
2. If they don't know the model id, propose a command that lists models, with the key referenced by variable, never typed out:
   `curl -sS -H "Authorization: Bearer $HPC_LLM_KEY" "$BASE_URL/models"`, with `BASE_URL` set to the address they gave you
3. Write the endpoint with `write_file` to `NAME.json` in the working directory. Your file tools can't reach `~/.config/hoplon/`, and that is by design.
4. Tell the user the next steps, which only they can do:
   - Review the file, then bless it: `hoplon --bless ./NAME.json`. hoplon shows the URL, model and key source and asks them to confirm. A blessed endpoint can be used by name from where it is.
   - Optionally move it to `~/.config/hoplon/endpoints/NAME.json` first, and bless it there.
   - Try it: `hoplon -e NAME "hello"`.
   - To make it the default, set `"default_endpoint": "NAME"` in `~/.config/hoplon/config.json`. Only the user does this.
5. If the endpoint is edited later, hoplon refuses it until it is blessed again. That is expected.
