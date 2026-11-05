# Project library script hyper.publisher -- installed and overwritten by gateway-agent.
# Do not edit in Designer; change gateway_agent/ignition_scripts/publisher.py instead.
#
# Every IMS timer script calls tick() once per interval. tick() reads that IMS's tags,
# keeps good-quality values, and posts them to the gateway agent, which buffers to disk
# and publishes to AWS IoT. If the agent is unreachable, readings queue in gateway
# globals (bounded) and are retried on the next tick, so a project reload or a short
# agent restart does not lose data.
#
# Written to parse as both Jython 2.7 and Python 3 so the agent's test suite can check it.

AGENT_URL = __AGENT_BASE_URL__ + "/v1/telemetry"
# Grants telemetry posts only; the agent's admin endpoints use a different token.
INGEST_TOKEN = __INGEST_TOKEN__
MAX_PENDING_TICKS = 600  # per IMS; at a 1 s interval this is 10 minutes of readings
MAX_TICKS_PER_POST = 30
WARN_EVERY_MS = 60000

_STATE_KEY = "hyper.publisher.state"


def _state(imsKey):
	g = system.util.getGlobals()
	allStates = g.get(_STATE_KEY)
	if allStates is None:
		allStates = {}
		g[_STATE_KEY] = allStates
	st = allStates.get(imsKey)
	if st is None:
		st = {"version": None, "paths": [], "pending": [], "dropped": 0, "lastWarn": 0, "lastOk": 0}
		allStates[imsKey] = st
	return st


def _jsonValue(value):
	# Numbers and booleans go through as-is; anything else (dates, Java objects) as text.
	# The cloud knows each tag's data type from its tag_id, so no type marker is sent.
	if isinstance(value, (bool, int, long, float)):
		return value
	return unicode(value)


def _client():
	g = system.util.getGlobals()
	c = g.get("hyper.publisher.http")
	if c is None:
		c = system.net.httpClient(timeout=5000)
		g["hyper.publisher.http"] = c
	return c


def tick(imsKey, version, meta):
	st = _state(imsKey)
	if st["version"] != version:
		st["paths"] = [m[0] for m in meta]
		st["version"] = version

	nowMs = system.date.now().getTime()
	values = system.tag.readBlocking(st["paths"])
	rows = []
	bad = 0
	for m, qv in zip(meta, values):
		# A tag with no value (for example a memory tag that never read a device) reports Good
		# quality with a null value; count it as bad rather than send a placeholder.
		if not qv.quality.isGood() or qv.value is None:
			bad += 1
			continue
		rows.append([m[1], _jsonValue(qv.value)])

	st["pending"].append({"ts": nowMs, "rows": rows, "bad": bad, "total": len(meta)})
	overflow = len(st["pending"]) - MAX_PENDING_TICKS
	if overflow > 0:
		del st["pending"][:overflow]
		st["dropped"] += overflow

	_flush(imsKey, version, st, nowMs)


def _flush(imsKey, version, st, nowMs):
	logger = system.util.getLogger("hyper.publisher")
	while st["pending"]:
		batch = st["pending"][:MAX_TICKS_PER_POST]
		body = system.util.jsonEncode({
			"ims": imsKey,
			"version": version,
			"dropped": st["dropped"],
			"ticks": batch,
		})
		try:
			resp = _client().post(AGENT_URL, data=body, headers={
				"Content-Type": "application/json", "X-Agent-Token": INGEST_TOKEN})
			ok = resp.good
		except Exception as e:
			ok = False
			resp = e
		if not ok:
			if nowMs - st["lastWarn"] > WARN_EVERY_MS:
				st["lastWarn"] = nowMs
				logger.warn("IMS %s: agent unreachable (%s); %d tick(s) queued, %d dropped" % (
					imsKey, resp, len(st["pending"]), st["dropped"]))
			return
		del st["pending"][:len(batch)]
		st["dropped"] = 0
		st["lastOk"] = nowMs
