"""
Vision QA — sends screenshots to Ollama Llama 3.2 Vision for evaluation.
"""

import base64
import logging
import requests

log = logging.getLogger("visual_qa.vision")

OLLAMA_URL = "http://127.0.0.1:11434"
VISION_MODEL = "llama3.2-vision:11b"
TIMEOUT = 600  # 10 min — model is slow on CPU


def evaluate_screenshot(image_path, expected_description, ticket_title=""):
    """
    Send a screenshot to the Vision model and ask if it matches expectations.
    Returns dict: verdict (PASS/FAIL), reason, raw response.
    """
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")

    prompt = (
        "Jesteś testerem QA aplikacji webowej EthOS.\n"
        "Analizujesz screenshot UI po wykonaniu kroku testowego.\n\n"
        f"Ticket: {ticket_title}\n"
        f"Oczekiwany wynik: {expected_description}\n\n"
        "Oceń screenshot:\n"
        "1. Czy UI wygląda poprawnie? (brak rozjechanych elementów, brakujących treści, błędów)\n"
        "2. Czy oczekiwany wynik jest spełniony?\n"
        "3. Czy są widoczne błędy wizualne?\n\n"
        "Odpowiedz w formacie:\n"
        "VERDICT: PASS lub FAIL\n"
        "REASON: krótkie uzasadnienie po polsku"
    )

    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": VISION_MODEL,
                "prompt": prompt,
                "images": [img_b64],
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 512},
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()

        verdict = "PASS"
        reason = raw
        for line in raw.split("\n"):
            line_up = line.strip().upper()
            if line_up.startswith("VERDICT:"):
                v = line_up.replace("VERDICT:", "").strip()
                if "FAIL" in v:
                    verdict = "FAIL"
                elif "PASS" in v:
                    verdict = "PASS"
            elif line.strip().upper().startswith("REASON:"):
                reason = line.strip()[7:].strip()

        log.info("Vision QA: %s — %s", verdict, reason[:100])
        return {"verdict": verdict, "reason": reason, "raw": raw}

    except requests.exceptions.ConnectionError:
        log.error("Ollama not running at %s", OLLAMA_URL)
        return {"verdict": "ERROR", "reason": "Ollama nie jest uruchomiony", "raw": ""}
    except Exception as e:
        log.error("Vision QA error: %s", e)
        return {"verdict": "ERROR", "reason": str(e), "raw": ""}
