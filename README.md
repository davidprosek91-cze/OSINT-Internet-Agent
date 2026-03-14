# OSINT Internet Agent

OSINT (Open Source Intelligence) Internet Agent je nástroj pro automatizované získávání informací z internetu. Tento projekt je navržen tak, aby využíval AI modely pro provádění vyhledávacích úkolů, analýzy a generování reportů.

## Klíčové vlastnosti
- Použití modelů, jako je `kimi-k2:1t-cloud`, pro analýzu a úkoly.
- Automatizace práce s webovými stránkami a extrakce dat.
- Možnost rozšíření a přizpůsobení pro různé OSINT scénáře.

## Požadavky

- Python 3.7+
- Knihovny:
  - `openai`
  - `requests`
  - `pytesseract`
  - `selenium`
  - `webdriver-manager`

## Instalace

1. Naklonujte repository:
   ```bash
   git clone https://github.com/davidprosek91-cze/OSINT-Internet-Agent.git
   ```
   
2. Přejděte do složky projektu:
   ```bash
   cd OSINT-Internet-Agent
   ```

3. Nainstalujte závislosti:
   ```bash
   pip install -r requirements.txt
   ```

## Použití

1. Nastavte OpenAI API klíč (např. pro model kimi-k2:1t-cloud):
   ```bash
   export OPENAI_API_KEY="váš_api_klíč"
   ```

2. Spusťte skript:
   ```bash
   python3 internet_agent.py
   ```

3. Řiďte se instrukcemi a vygenerujte report.

## K čemu je vhodný

- Hledání informací o osobách, organizacích nebo specifických tématech.
- Extrakce veřejně dostupných informací z různých platforem (OSINT).
- Generování přehledných reportů z nasbíraných dat.

---

**Poznámka:** Používejte tento nástroj zodpovědně a v souladu s platnými zákony a předpisy o ochraně dat.
