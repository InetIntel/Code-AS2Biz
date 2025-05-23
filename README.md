# AS2Biz: Leveraging Web Presence and AI to Improve AS Business Classification

This repository accompanies the paper:

**AS2Biz: Leveraging Web Presence and AI to Improve AS Business Classification**

It contains:

- 📦 `datasets/`  
  Output datasets, including:
  - `AS2Web`: AS-to-website mappings
  - `AS2Biz`: AS-to-business classification results

- 🧪 `as2web_code/`  
  Source code for generating the AS2Web dataset from WHOIS, IPinfo, PeeringDB, and Perplexity AI.

- 🧠 `website_classification_prompts/`  
  The AI prompts used for classifying business sectors based on website content.

- 📊 `as2web_as2biz_analysis.ipynb`  
  A Jupyter notebook for analyzing the AS2Web and AS2Biz datasets and reproducing key statistics reported in the paper.

---

## 🔧 AS2Web Generation Pipeline

We provide detailed step-by-step instructions in:

📁 [`as2web_code/README.md`](./as2web_code/README.md)

This guide walks you through:

- Downloading and querying WHOIS, IPinfo, and PeeringDB data
- Running Perplexity AI queries for ASes without accessible websites
- Producing the final `as2web.json` dataset

---

## 🌐 Website HTML Archive

We also release the raw HTML content scraped from AS websites, used for classification and validation.

📂 You can access this archive via OneDrive:  
**[Download from OneDrive (link)](xxx)**

---

Please cite our paper if you use the dataset or code.
