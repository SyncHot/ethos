import os
from crewai import Agent, Task, Crew
from playwright.sync_api import sync_playwright

def autonomous_qa_test(username, password):
    results = []
    with sync_playwright() as p:
        print("🚀 Agent wchodzi do akcji...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 720})
        page = context.new_page()
        
        try:
            # 1. LOGOWANIE
            page.goto("http://localhost:9000", timeout=10000)
            page.fill('input[name="username"], #username', username)
            page.fill('input[name="password"], #password', password)
            page.click('button[type="submit"], button:has-text("Zaloguj")')
            page.wait_for_load_state("networkidle")
            
            results.append(f"Zalogowano. Aktualny URL: {page.url}")

            # 2. KREATYWNA EKSPLORACJA (Video Player)
            print("🕵️ Szukam Video Playera i testuję...")
            
            # Agent szuka linku do playera
            video_link = page.locator('a:has-text("Video"), a[href*="video"], .video-player-link')
            if video_link.count() > 0:
                video_link.first.click()
                page.wait_for_timeout(2000)
                results.append("Przejście do sekcji Video: Sukces")
            
            # --- TESTY KREATYWNE (Edge Cases) ---
            
            # Test 1: Szybkie klikanie (Stress Test)
            print("⚡ Testuję szybkie przełączanie...")
            play_button = page.locator('button:has-text("Play"), .play-btn').first
            if play_button.is_visible():
                for _ in range(5):
                    play_button.click()
                    page.wait_for_timeout(200)
                results.append("Test 'Multi-click Play': Aplikacja nie scrashowała")

            # Test 2: Brakujące wideo / Błędy ładowania
            logs = []
            page.on("console", lambda msg: logs.append(f"Console {msg.type}: {msg.text}"))
            page.wait_for_timeout(3000) # Czekamy na ewentualne błędy w konsoli
            
            if any("404" in log or "Error" in log for log in logs):
                results.append(f"Znaleziono błędy w konsoli: {logs}")

            # Test 3: Rozmiar okna (Responsywność)
            page.set_viewport_size({"width": 375, "height": 667}) # iPhone SE size
            page.wait_for_timeout(1000)
            results.append("Test responsywności (mobile): Wykonany")

        except Exception as e:
            results.append(f"Krytyczny błąd podczas eksploracji: {str(e)}")
        finally:
            browser.close()
            
    return "\n".join(results)

# --- CREW AI LOGIC ---

creative_qa = Agent(
    role='Kreatywny Pentester UI/UX',
    goal='Znaleźć błędy w Video Playerze, których nie przewidział twórca',
    backstory='Jesteś złośliwym testerem. Klikasz tam, gdzie nie wolno, zmieniasz rozmiar okna w trakcie ładowania i sprawdzasz czy konsola sypie błędami.',
    verbose=True
)

exploration_task = Task(
    description=(
        "Przeanalizuj wyniki zautomatyzowanej sesji na localhost:9000. "
        "Użytkownik: marcin, Hasło: pluton2303. "
        "Skup się na Video Playerze: czy wideo startuje, czy przyciski reagują, "
        "czy zmiana rozmiaru okna psuje układ i czy w konsoli są błędy JS."
    ),
    expected_output="Szczegółowa lista znalezionych 'dziwnych' zachowań i błędów technicznych.",
    agent=creative_qa
)

# Uruchomienie sesji i przekazanie danych do Agenta
print("--- URUCHAMIAM AUTONOMICZNĄ SESJĘ TESTOWĄ ---")
raw_data = autonomous_qa_test("marcin", "pluton2303")

crew = Crew(agents=[creative_qa], tasks=[exploration_task])
final_report = crew.kickoff(inputs={'raw_data': raw_data})

print("\n--- RAPORT KOŃCOWY AGENTA ---")
print(final_report)