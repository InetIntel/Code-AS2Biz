# AS2Biz Dataset

AS2Biz is an Autonomous System (AS) business classification dataset. It classifies ASes by the business sectors of their operating organizations using web presence, primarily through the organization’s official website and sibling-AS-based augmentation, while supplementing missing cases with fallback methods such as Wikipedia-based classification and AI-based organization classification derived directly from web search.

## Overview

For each snapshot month, the dataset stores:
- metadata about the snapshot and models used
- a mapping from ASN to one or more business-type labels
- the provenance of each assigned label

The JSON structure is:

```json
{
  "metadata": {
    "snapshot_month": "{yyyy_mm}",
    "web_classification_llm": "{model1}",
    "web_search_llm": "{model2}"
  },
  "data": {
    "AS1": {
      "{business type 1}": "{source}",
      "{business type 2}": "{source}"
    },
    "AS2": {
      "{business type 3}": "{source}"
    }
  }
}
```

## Field Description

### `metadata`

- `snapshot_month`: snapshot month in `YYYY-MM` format, for example `2025-09`
- `web_classification_llm`: the main LLM used to classify scraped website content into business categories
- `web_search_llm`: the search-oriented LLM used for website discovery and fallback classification

Example:

```json
{
  "metadata": {
    "snapshot_month": "2025-09",
    "web_classification_llm": "gpt-4o",
    "web_search_llm": "Perplexity AI sonar-pro"
  }
}
```

### `data`

`data` is a dictionary keyed by ASN string. Each ASN maps to a dictionary whose keys are business-type labels and whose values record the source of that label.

Example:

```json
{
  "data": {
    "12345": {
      "Computer and Information Technology - Internet Service Provider (ISP)": "Direct - Website",
      "Computer and Information Technology - Phone Provider": "Inherit from AS67890"
    },
  }
}
```

## Source Values

Each business label includes its provenance. Source values are:

- `Direct - Website`  
  Assigned directly from the organization's official website after scraping and LLM classification.

- `Inherit from ...`  
  Indicates that the business type is inherited from sibling ASes belonging to the same organization. Only sibling labels with source Direct - Website are considered for inheritance, and only when this ASN does not already have the same business type assigned directly. For example, if an ASN already has the label ISP from its own website classification, then the same ISP label from sibling AS12345 is not added as Inherit from AS12345.

- `Direct - Wikipedia`  
  Assigned from Wikipedia scraping and classification in the fallback stage.

- `Direct - Fallback Web Search AI`  
  Assigned from AI-based web search and classification in the fallback stage.

## Taxonomy

AS2Biz uses a multi-label business taxonomy derived from ASdb's NAICSlite taxonomy. We keep most of ASdb's NAICSlite taxonomy, but the **Agriculture, Mining, and Refineries** sector is expanded into finer subcategories because the original taxonomy is too coarse there.

```python
taxonomy = [
    "Computer and Information Technology - Internet Service Provider (ISP)",
    "Computer and Information Technology - Phone Provider",
    "Computer and Information Technology - IP Transit",
    "Computer and Information Technology - Hosting, Cloud Provider, Data Center, Server Colocation",
    "Computer and Information Technology - Computer and Network Security",
    "Computer and Information Technology - Software Development",
    "Computer and Information Technology - Technology Consulting Services",
    "Computer and Information Technology - Satellite Communication",
    "Computer and Information Technology - Search Engine",
    "Computer and Information Technology - Internet Exchange Point (IXP)",
    "Computer and Information Technology - Other",
    "Media, Publishing, and Broadcasting - Online Music and Video Streaming Services",
    "Media, Publishing, and Broadcasting - Online Informational Content",
    "Media, Publishing, and Broadcasting - Print Media (Newspapers, Magazines, Books)",
    "Media, Publishing, and Broadcasting - Music and Video Industry",
    "Media, Publishing, and Broadcasting - Radio and Television Providers",
    "Media, Publishing, and Broadcasting - Other",
    "Finance and Insurance - Banks, Credit Card Companies, Mortgage Providers",
    "Finance and Insurance - Insurance Carriers and Agencies",
    "Finance and Insurance - Accountants, Tax Preparers, Payroll Services",
    "Finance and Insurance - Investment, Portfolio Management, Pensions and Funds",
    "Finance and Insurance - Other",
    "Education and Research - Elementary and Secondary Schools",
    "Education and Research - Colleges, Universities, and Professional Schools",
    "Education and Research - Other Schools, Instruction, and Exam Preparation (Trade Schools, Art Schools, Driving Instruction, etc.)",
    "Education and Research - Research and Development Organizations",
    "Education and Research - Education Software",
    "Education and Research - Other",
    "Service - Law, Business, and Consulting Services",
    "Service - Buildings, Repair, Maintenance (Pest Control, Landscaping, Cleaning, Locksmiths, Car Washes, etc)",
    "Service - Personal Care and Lifestyle (Barber Shops, Nail Salons, Diet Centers, Laundry, etc)",
    "Service - Social Assistance (Temporary Shelters, Emergency Relief, Child Day Care, etc)",
    "Service - Other",
    "Agriculture, Mining, and Refineries - Agriculture & Farming",
    "Agriculture, Mining, and Refineries - Forestry & Wood",
    "Agriculture, Mining, and Refineries - Fisheries & Aquaculture",
    "Agriculture, Mining, and Refineries - Mining & Minerals",
    "Agriculture, Mining, and Refineries - Oil & Gas",
    "Agriculture, Mining, and Refineries - Refineries & Primary Processing",
    "Agriculture, Mining, and Refineries - Other",
    "Community Groups and Nonprofits - Churches and Religious Organizations",
    "Community Groups and Nonprofits - Human Rights and Social Advocacy (Human Rights, Environment and Wildlife Conservation, Other)",
    "Community Groups and Nonprofits - Other",
    "Construction and Real Estate - Buildings (Residential or Commercial)",
    "Construction and Real Estate - Civil Eng. Construction (Utility Lines, Roads and Bridges)",
    "Construction and Real Estate - Real Estate (Residential and/or Commercial)",
    "Construction and Real Estate - Other",
    "Museums, Libraries, and Entertainment - Libraries and Archives",
    "Museums, Libraries, and Entertainment - Recreation, Sports, and Performing Arts",
    "Museums, Libraries, and Entertainment - Amusement Parks, Arcades, Fitness Centers, Other",
    "Museums, Libraries, and Entertainment - Museums, Historical Sites, Zoos, Nature Parks",
    "Museums, Libraries, and Entertainment - Casinos and Gambling",
    "Museums, Libraries, and Entertainment - Tours and Sightseeing",
    "Museums, Libraries, and Entertainment - Other",
    "Utilities (Excluding Internet Service) - Electric Power Generation, Transmission, Distribution",
    "Utilities (Excluding Internet Service) - Natural Gas Distribution",
    "Utilities (Excluding Internet Service) - Water Supply and Irrigation",
    "Utilities (Excluding Internet Service) - Sewage Treatment",
    "Utilities (Excluding Internet Service) - Steam and Air-Conditioning Supply",
    "Utilities (Excluding Internet Service) - Other",
    "Health Care Services - Hospitals and Medical Centers",
    "Health Care Services - Medical Laboratories and Diagnostic Centers",
    "Health Care Services - Nursing, Residential Care Facilities, Assisted Living, and Home Health Care",
    "Health Care Services - Other",
    "Travel and Accommodation - Air Travel",
    "Travel and Accommodation - Railroad Travel",
    "Travel and Accommodation - Water Travel",
    "Travel and Accommodation - Hotels, Motels, Inns, Other Traveler Accommodation",
    "Travel and Accommodation - Recreational Vehicle Parks and Campgrounds",
    "Travel and Accommodation - Boarding Houses, Dormitories, Workers' Camps",
    "Travel and Accommodation - Food Services and Drinking Places",
    "Travel and Accommodation - Other",
    "Freight, Shipment, and Postal Services - Postal Services and Couriers",
    "Freight, Shipment, and Postal Services - Air Transportation",
    "Freight, Shipment, and Postal Services - Railroad Transportation",
    "Freight, Shipment, and Postal Services - Water Transportation",
    "Freight, Shipment, and Postal Services - Trucking",
    "Freight, Shipment, and Postal Services - Space, Satellites",
    "Freight, Shipment, and Postal Services - Passenger Transit (Car, Bus, Taxi, Subway)",
    "Freight, Shipment, and Postal Services - Other",
    "Government and Public Administration - Military, Defense, National Security, and Intl. Affairs",
    "Government and Public Administration - Law Enforcement, Public Safety, and Justice",
    "Government and Public Administration - Government and Regulatory Agencies, Administrations, Departments, and Services",
    "Government and Public Administration - Other",
    "Retail Stores, Wholesale, and E-commerce Sites - Food, Grocery, Beverages",
    "Retail Stores, Wholesale, and E-commerce Sites - Clothing, Fashion, Luggage",
    "Retail Stores, Wholesale, and E-commerce Sites - Other",
    "Manufacturing - Automotive and Transportation",
    "Manufacturing - Food, Beverage, and Tobacco",
    "Manufacturing - Clothing and Textiles",
    "Manufacturing - Machinery",
    "Manufacturing - Chemical and Pharmaceutical Manufacturing",
    "Manufacturing - Electronics and Computer Components",
    "Manufacturing - Metal, Glass, Wood, and Paper Manufacturing",
    "Manufacturing - Other",
    "Other - Individually Owned"
]
```

# AS2Web Dataset

For each snapshot month, the dataset stores:
- metadata about the snapshot and models used
- a mapping from ASN to its website URL
- the provenance of each assigned URL

The JSON structure is:

```json
{
  "metadata": {
    "snapshot_month": "{yyyy_mm}",
    "web_search_setting": "{model}",
  },
  "data": {
    "AS1": {
      "url": "{url}",
      "sources": "{sources}"
    },
  }
}
```

## Source Values

Source values are:

- `Whois`  
  The website comes from Whois.

- `PeeringDB`  
  The website comes from PeeringDB.

- `IPinfo`  
  The website comes from IPinfo.

- `WebSearch`  
  The website comes from AI-based web search.
