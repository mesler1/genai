# hoplon_core/client.py: the model endpoint (a handler: the model request).
#
# Endpoint (a profile's server, model and key) and Client, which sends a conversation and reads the reply: no
# redirects, no proxies unless asked, replies capped in size.
#
# Uses: base

def _is_loopback(host):
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class Endpoint:
    """Where requests go and how they are authenticated."""

    def __init__(self, data, path):
        self.path = path
        self.data = data
        if not isinstance(data, dict):
            raise HoplonError(f"{path}: an endpoint must be a JSON object", EXIT_PROFILE)
        unknown = set(data) - ENDPOINT_KEYS
        if unknown:
            hint = " (persona fields belong in a profile, under profiles/)" if unknown & PROFILE_KEYS else ""
            raise HoplonError(f"{path}: unknown endpoint fields: {', '.join(sorted(unknown))}{hint}", EXIT_PROFILE)
        for k in ("name", "base_url", "model"):
            if not isinstance(data.get(k), str) or not data[k]:
                raise HoplonError(f"{path}: '{k}' is required", EXIT_PROFILE)
        self.name, self.base_url = data["name"], data["base_url"].rstrip("/")
        self.aliases = data.get("aliases", {})
        if not (isinstance(self.aliases, dict) and all(isinstance(k, str) and ALIAS_NAME.fullmatch(k)
                                                       and isinstance(v, str) and MODEL_ID.fullmatch(v)
                                                       for k, v in self.aliases.items())):
            raise HoplonError(f"{path}: 'aliases' must map short names (a letter, then letters, digits, '_', '-') "
                              "to the server's model names", EXIT_PROFILE)
        if "auto" in self.aliases:
            raise HoplonError(f"{path}: 'auto' can't be an alias; it means the server's first model", EXIT_PROFILE)
        self.model = self.resolve(data["model"])        # "model" may name an alias too
        if "key_env" in data and "key_file" in data:
            raise HoplonError(f"{path}: give key_env or key_file, not both", EXIT_PROFILE)
        self.key_env, self.key_file = data.get("key_env"), data.get("key_file")
        for k in ("key_file", "ca_bundle", "proxy"):
            if k in data and not (isinstance(data[k], str) and data[k]):
                raise HoplonError(f"{path}: '{k}' must be a non-empty string", EXIT_PROFILE)
        if not isinstance(data.get("description", ""), str):
            raise HoplonError(f"{path}: 'description' must be a string", EXIT_PROFILE)
        if self.key_env is not None and not (isinstance(self.key_env, str) and ENV_NAME_RE.fullmatch(self.key_env)):
            raise HoplonError(f"{path}: 'key_env' must be a plain variable name (letters, digits, underscore)",
                             EXIT_PROFILE)
        self.context_limit = data.get("context_limit", 32000)
        if type(self.context_limit) is not int or not 1 <= self.context_limit <= CONTEXT_LIMIT_MAX:
            raise HoplonError(f"{path}: 'context_limit' must be a whole number of tokens from 1 to "
                             f"{CONTEXT_LIMIT_MAX:,}", EXIT_PROFILE)
        self.blessed_limit = self.context_limit     # for a model whose size the server doesn't say, an alias's too
        self.timeout = data.get("timeout", 120)
        if type(self.timeout) not in (int, float) or not (math.isfinite(self.timeout) and
                                                          TIMEOUT_RANGE[0] <= self.timeout <= TIMEOUT_RANGE[1]):
            raise HoplonError(f"{path}: 'timeout' must be a number of seconds from {TIMEOUT_RANGE[0]} to "
                             f"{TIMEOUT_RANGE[1]}", EXIT_PROFILE)
        self.timeout = float(self.timeout)
        self.ca_bundle = data.get("ca_bundle")
        self.headers = data.get("headers", {})
        self.params = data.get("params", {})
        if not isinstance(self.params, dict):
            raise HoplonError(f"{path}: 'params' must be an object of request settings", EXIT_PROFILE)
        self.params = dict(self.params)
        self.allow_http = data.get("allow_http", False)
        if type(self.allow_http) is not bool:
            raise HoplonError(f"{path}: 'allow_http' must be true or false", EXIT_PROFILE)
        self.proxy = data.get("proxy")
        self.token = None       # remote mode: the session token the key proxy requires (see remote_config)
        if not isinstance(self.headers, dict) or not all(isinstance(v, str) for v in self.headers.values()):
            raise HoplonError(f"{path}: 'headers' must map names to strings", EXIT_PROFILE)
        if any(k.lower() in ("authorization", "proxy-authorization", "cookie") for k in self.headers):
            raise HoplonError(f"{path}: credentials don't belong in an endpoint's headers; use key_env or key_file",
                             EXIT_PROFILE)
        for k in ("model", "messages", "tools"):
            if k in self.params:
                raise HoplonError(f"{path}: params may not set '{k}'", EXIT_PROFILE)
        if type(self.params.get("stream", True)) is not bool:
            raise HoplonError(f"{path}: params' 'stream' must be true or false", EXIT_PROFILE)
        u = urllib.parse.urlsplit(self.base_url)
        if u.scheme not in ("https", "http") or not u.hostname:
            raise HoplonError(f"{path}: base_url must be an http(s) URL", EXIT_PROFILE)
        if u.username or u.password:
            raise HoplonError(f"{path}: base_url must not contain credentials", EXIT_PROFILE)
        if u.scheme == "http" and not (_is_loopback(u.hostname) or self.allow_http):
            raise HoplonError(f"{path}: plain HTTP to {u.hostname} needs \"allow_http\": true", EXIT_PROFILE)
        if self.proxy is not None:
            pu = urllib.parse.urlsplit(str(self.proxy))
            if pu.scheme not in ("http", "https") or not pu.hostname:
                raise HoplonError(f"{path}: 'proxy' must be an http(s) URL", EXIT_PROFILE)

    @classmethod
    def load(cls, path):
        return cls.parse(read_bytes_or_missing(path, "endpoint"), path)

    @classmethod
    def parse(cls, raw, path):
        return cls(parse_json_bytes(raw, path), os.path.abspath(path))

    def resolve(self, name):
        """The server's model name for name: an alias of this endpoint is looked up first."""
        return self.aliases.get(name, name)

    def alias_of(self, model=None):
        """This endpoint's alias for a model (the current one by default), or None."""
        model = self.model if model is None else model
        return next((k for k, v in self.aliases.items() if v == model), None)

    def shown_model(self, model=None):
        """The model as hoplon shows it: "high (pro-model)" for an alias, with a long server name shortened."""
        model = self.model if model is None else model
        alias = self.alias_of(model)
        return f"{alias} ({short_model(model)})" if alias else model

    def destination_id(self):
        """Fingerprint where a conversation goes, without storing endpoint headers or proxy credentials: the server
        (scheme, host and port), and the proxy, CA bundle and headers that reach it. Not the model or the URL's
        path, so switching models on the same server doesn't need consent each time."""
        u = urllib.parse.urlsplit(self.base_url)
        port = u.port or {"https": 443, "http": 80}.get(u.scheme)
        fields = [u.scheme, (u.hostname or "").lower(), port, self.proxy, self.ca_bundle, self.headers]
        return hash_bytes(json.dumps(fields, sort_keys=True).encode())

    def key_source(self):
        """Where the key comes from, escaped for one line. Returns (text, flagged)."""
        if self.key_env:
            d, f = one_line(self.key_env)
            return f"environment variable ${d}", f
        if self.key_file:
            d, f = one_line(self.key_file)
            return f"file {d} (must be mode 0600)", f
        return "none (no Authorization header is sent)", False

    def key(self):
        if self.key_env:
            v = os.environ.get(self.key_env)
            if not v:
                raise HoplonError(f"endpoint '{self.name}' reads its key from ${self.key_env}, which is not set",
                                 EXIT_PROFILE)
            return v.strip()
        if self.key_file:
            p = os.path.expanduser(self.key_file)
            try:
                st = os.stat(p)
            except OSError as e:
                raise HoplonError(f"key file {p}: {e.strerror}", EXIT_PROFILE)
            if st.st_mode & 0o077:
                raise HoplonError(f"key file {p} must be mode 0600 (it is {stat.S_IMODE(st.st_mode):04o})",
                                 EXIT_PROFILE)
            if st.st_uid != os.getuid():
                raise HoplonError(f"key file {p} is not owned by you", EXIT_PROFILE)
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        return None

    def summary(self):
        """The review screen, every field escaped to one line. Returns (text, flagged)."""
        flags = []

        def one(v):
            d, f = one_line(str(v))
            flags.append(f)
            return d

        url = one(self.base_url)
        host = urllib.parse.urlsplit(self.base_url).hostname or ""
        if not host.isascii():
            try:
                url += f"  (punycode: {host.encode('idna').decode('ascii')})"
            except UnicodeError:
                url += "  (this host name cannot be converted to punycode)"
        key, kflag = self.key_source()
        lines = [f"  endpoint: {one(self.name)}", f"  url:      {url}", f"  model:    {one(self.shown_model())}",
                 f"  key from: {key}", f"  context:  {self.context_limit} tokens"]
        flags.append(kflag)
        if self.allow_http:
            lines.append("  [!] allow_http: prompts, files and the key may travel unencrypted")
        if self.proxy:
            lines.append(f"  proxy:    {one(self.proxy)}")
        if self.ca_bundle:
            lines.append(f"  CA:       {one(self.ca_bundle)}")
        if self.headers:
            lines.append(f"  headers:  {', '.join(one(k) for k in sorted(self.headers))}")
        if self.params:
            lines.append(f"  params:   {json.dumps(self.params, sort_keys=True)}")
        for k in sorted(self.aliases):
            lines.append(f"  alias:    {one(k)} sends to {one(self.aliases[k])}")
        return "\n".join(lines), any(flags)

    def info(self):
        return {"name": self.name, "base_url": self.base_url, "model": self.model, "alias": self.alias_of()}


def alias_hint(ep, name):
    """What to add when a name that looks like an alias isn't one of this endpoint's."""
    if not name or not ALIAS_NAME.fullmatch(name):
        return ""
    have = f" (it has: {', '.join(sorted(ep.aliases))})" if ep.aliases else ""
    return f"{have}; add \"aliases\": {{\"{name}\": \"MODEL\"}} to the endpoint file and bless it again"


def short_model(model, n=32):
    """A server's model name, shortened for display: its last path part, cut to n characters."""
    tail = model.rsplit("/", 1)[-1] or model
    return tail if len(tail) <= n else tail[:n - 1] + "…"


# --------------------------------------------------------------------------
# HTTP client

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HoplonError(f"the endpoint answered with a redirect ({code}) to {newurl}; redirects are not "
                         "followed. Nothing more was sent.", EXIT_API, "api")


def too_big():
    return HoplonError(f"the endpoint's reply exceeded the {REPLY_CAP // (1024 * 1024)} MB limit; "
                      "nothing was performed", EXIT_API, "api")


class Client:
    def __init__(self, endpoint, params=None):
        self.p = endpoint
        self.params = {**endpoint.params, **(params or {})}    # a profile's params override the endpoint's
        self._key = endpoint.key()
        ctx = ssl.create_default_context(cafile=os.path.expanduser(endpoint.ca_bundle) if endpoint.ca_bundle else None)
        proxies = {"http": endpoint.proxy, "https": endpoint.proxy} if endpoint.proxy else {}
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(proxies),  # environment proxies are ignored
            urllib.request.HTTPSHandler(context=ctx), NoRedirect())
        # Each chat request and its reply, exactly as sent and received, in memory only: a wrapper can show
        # them (the trace message). Header values that may be secret are left out.
        self.trace = collections.deque(maxlen=TRACE_KEEP)
        self.exchanges = 0
        self._ex = None

    def _request(self, path, body=None, trace=False):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "User-Agent": f"hoplon/{VERSION}"}
        headers.update(self.p.headers)
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        if self.p.token:
            headers[TOKEN_HEADER] = self.p.token
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.p.base_url + path, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
        if trace:
            self.exchanges += 1
            shown = {k: "Bearer (your API key, not shown)" if k == "Authorization" else
                     "(set in the endpoint; not shown)" if k in self.p.headers or (k == TOKEN_HEADER and self.p.token)
                     else v for k, v in headers.items()}
            self._ex = {"n": self.exchanges, "time": now(), "method": req.get_method(), "url": req.full_url,
                        "headers": shown, "request": data.decode("utf-8", "replace") if data else "",
                        "status": "sending", "response": [], "response_bytes": 0, "started": time.time()}
            self.trace.append(self._ex)
        try:
            r = self.opener.open(req, timeout=self.p.timeout)
            if trace:
                self._ex["http"] = r.status
            return r
        except urllib.error.HTTPError as e:
            detail = e.read(2000).decode("utf-8", "replace").strip()
            if trace:
                self._ex["http"] = e.code
                self._got(detail)
            raise HoplonError(f"endpoint returned HTTP {e.code}: {detail[:500]}", EXIT_API, "api")
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise HoplonError(f"cannot reach {self.p.base_url}: {reason}", EXIT_API, "api")

    def models(self):
        with self._request("/models") as r:
            data = r.read(MODELS_CAP + 1)
        if len(data) > MODELS_CAP:
            raise HoplonError("the server's model list is too large", EXIT_API, "api")
        try:
            return json.loads(data)
        except ValueError:
            raise HoplonError("the server's model list is not JSON", EXIT_API, "api") from None

    def model_list(self):
        """The models the server lists (GET /models), as [(name, context length or None)]. Names outside MODEL_ID
        are left out; vLLM's max_model_len (or a context_length) is taken as the context limit."""
        data = self.models()
        out = []
        for m in (data.get("data") if isinstance(data, dict) else None) or []:
            mid = m.get("id") if isinstance(m, dict) else None
            if isinstance(mid, str) and MODEL_ID.fullmatch(mid) and mid not in (x for x, _ in out):
                n = m.get("max_model_len", m.get("context_length"))
                out.append((mid, n if type(n) is int and n > 0 else None))
        return out[:MODELS_KEEP]

    def _got(self, text):
        """Keep what the endpoint sent, as it came, up to the reply cap."""
        ex = self._ex
        if ex is not None and ex["response_bytes"] <= REPLY_CAP:
            ex["response"].append(text)
            ex["response_bytes"] += len(text)

    def chat(self, messages, tools, on_text, max_tokens=None, on_progress=None):
        """Send one request. Returns (message, usage). on_progress, if given, hears what is arriving while the
        reply streams (progress_report), about twice a second; self.generated is then what the reply took."""
        self._ex = None
        self.generated = None
        try:
            msg, usage = self._chat(messages, tools, on_text, max_tokens, on_progress)
        except HoplonError as e:
            if self._ex is not None:
                self._ex.update(status="error", error=str(e), ms=int(1000 * (time.time() - self._ex["started"])))
            raise
        if self._ex is not None:
            self._ex.update(status="ok", message=msg, usage=usage, ms=int(1000 * (time.time() - self._ex["started"])))
        return msg, usage

    def _chat(self, messages, tools, on_text, max_tokens=None, on_progress=None):
        params = dict(self.params)
        stream = bool(params.pop("stream", True))
        body = {"model": self.p.model, "messages": messages, "stream": stream}
        body.update(params)
        if tools:
            body["tools"] = tools
        if max_tokens:
            body["max_tokens"] = max_tokens
        started = time.time()
        with self._request("/chat/completions", body, trace=True) as r:
            if self._ex is not None:
                self._ex["stream"] = stream
            if not stream:
                raw = r.read(REPLY_CAP + 1)
                self._got(raw.decode("utf-8", "replace"))
                if len(raw) > REPLY_CAP:
                    raise too_big()
                data = json.loads(raw)
                if "choices" not in data or not data["choices"]:
                    raise HoplonError(f"unexpected reply from endpoint: {str(data)[:300]}", EXIT_API, "api")
                msg = data["choices"][0].get("message", {})
                if msg.get("content"):
                    on_text(msg["content"])
                done = completion_tokens(data.get("usage"))
                if done is None:        # no usage reported: estimated from the reply, so every request is counted
                    calls = json.dumps(msg.get("tool_calls") or "")
                    done, guessed = max(1, (len(msg.get("content") or "") + len(calls)) // 4), True
                else:
                    guessed = False
                self.generated = {"tokens": done, "estimated": guessed, "seconds": time.time() - started}
                return self._clean(msg.get("content"), msg.get("tool_calls") or []), data.get("usage")
            return self._stream(r, on_text, on_progress)

    def _stream(self, r, on_text, on_progress=None):
        content, calls, usage, size = [], {}, None, 0
        pieces, thinking, first, told = 0, 0, None, 0.0   # streamed pieces (about a token each), of them thinking
        while True:
            raw = r.readline(REPLY_CAP + 1)
            size += len(raw)
            if size > REPLY_CAP or len(raw) > REPLY_CAP:
                raise too_big()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._got(line)
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if chunk.get("error"):
                raise HoplonError(f"endpoint error: {chunk['error']}", EXIT_API, "api")
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk.get("choices") or []:
                delta = ch.get("delta") or {}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if delta.get("content") or reasoning or delta.get("tool_calls"):
                    pieces += 1
                    thinking += bool(reasoning)
                    first = first or time.time()
                if delta.get("content"):
                    content.append(delta["content"])
                    on_text(delta["content"])
                else:
                    on_text("")             # reasoning or a tool call: no text, but a stop is seen here too
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": []})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["args"].append(fn["arguments"])
                        slot["size"] = slot.get("size", 0) + len(fn["arguments"].encode("utf-8"))
                if on_progress and time.time() - told >= PROGRESS_EVERY and pieces:
                    told = time.time()
                    on_progress(progress_report(reasoning, delta, calls, pieces, thinking))
        done = completion_tokens(usage)
        if pieces or done is not None:
            self.generated = {"tokens": done if done is not None else pieces, "estimated": done is None,
                              "seconds": time.time() - first if first else 0.0}
        tool_calls = [{"id": s["id"] or f"call_{i}", "type": "function",
                       "function": {"name": s["name"], "arguments": "".join(s["args"])}}
                      for i, s in sorted(calls.items())]
        return self._clean("".join(content) or None, tool_calls), usage

    @staticmethod
    def _clean(content, tool_calls):
        msg = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = [{"id": tc.get("id") or f"call_{i}", "type": "function",
                                  "function": {"name": tc["function"]["name"],
                                               "arguments": tc["function"].get("arguments") or "{}"}}
                                 for i, tc in enumerate(tool_calls)]
        return msg
