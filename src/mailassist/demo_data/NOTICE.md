# Demo-Daten – Herkunft und Lizenzen

Alle Daten in diesem Ordner sind **synthetisch**. Sie enthalten keine echten Personen, Kunden oder Postfächer.

## `beispiel_gmbh.toml`

21 synthetische B2B-Mails der fiktiven „Beispiel Maschinenbau GmbH“, für dieses Projekt geschrieben. Teil dieses Repositorys, gleiche Lizenz wie das Projekt.

## `gretel_de.json`

27 deutsche Datensätze aus **gretelai/synthetic_pii_finance_multilingual** (Gretel.ai), lizenziert unter der **Apache License 2.0** (<https://www.apache.org/licenses/LICENSE-2.0>). Die Daten enthalten *künstliche* personenbezogene Angaben (Namen, Adressen, Konto- und Kundennummern), die von Gretel Navigator erzeugt wurden.

- Quelle: <https://huggingface.co/datasets/gretelai/synthetic_pii_finance_multilingual> (Datei `data/German_train-00000-of-00001.parquet`)
- Zitat laut Datensatz:

  ```
  @software{gretel-synthetic-pii-finance-multilingual-2024,
    author = {Watson, Alex and Meyer, Yev and Van Segbroeck, Maarten and Grossman, Matthew and Torbey, Sami and Mlocek, Piotr and Greco, Johnny},
    title = {{Synthetic-PII-Financial-Documents-North-America}: A synthetic dataset for training language models to label and detect PII in domain specific formats},
    month = {June},
    year = {2024},
    url = {https://huggingface.co/datasets/gretelai/synthetic_pii_finance_multilingual}
  }
  ```

**Änderungen gegenüber dem Original** (erzeugt mit `scripts/build_demo_data.py`):

1. Auswahl: Dokumenttyp „Email“ (mit Betreffzeile) und „Customer support conversational log“ (nur sauber in Kunde/Support zerlegbare Verläufe), `quality_score` ≥ 85, 300–2500 Zeichen, die ersten 26 bzw. 10 in Datensatz-Reihenfolge.
2. Umformung in Mail-Struktur: Betreff und Absender aus den Kopfzeilen; fehlende Absender durch `absender-N@gretel-demo.example` ersetzt; Support-Verläufe in eingehende Mail + Verlauf zerlegt.
3. E-Mail-Domains und URL-Hosts, die keine reservierten Beispiel-Domains sind, wurden zu `*.example` umgeschrieben (z. B. `gmail.com` → `gmail-com.example`), damit die Demo keine echten Domains enthält.
