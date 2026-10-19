# -*- coding: utf-8 -*-
"""Клиент OpenAI-совместимого API: DeepSeek, OpenRouter, YandexGPT, OpenAI — меняется в config.json."""
import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("llm")


class LLM:
    def __init__(self, base_url, api_key, model, proxy=None, temperature=0.3):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.key = api_key
        self.model = model
        self.temperature = temperature
        handlers = [urllib.request.ProxyHandler({"https": proxy, "http": proxy})] if proxy else []
        self.opener = urllib.request.build_opener(*handlers)

    def chat(self, messages, tools=None):
        """Один запрос. Возвращает message из первого choice (content и/или tool_calls)."""
        body = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
        )
        for attempt in range(3):
            try:
                with self.opener.open(req, timeout=90) as r:
                    data = json.loads(r.read())
                return data["choices"][0]["message"]
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")[:500]
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    log.warning("LLM %s, повтор: %s", e.code, text)
                    time.sleep(2 + attempt * 3)
                    continue
                raise RuntimeError("LLM HTTP %s: %s" % (e.code, text))
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 2:
                    log.warning("LLM сеть: %s, повтор", e)
                    time.sleep(2 + attempt * 3)
                    continue
                raise
