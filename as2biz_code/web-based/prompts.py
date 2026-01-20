main_dev_prompt = """
You are tasked with analyzing the content of a given organization's website to classify the organization into one or more business categories.
Carefully review the website's explicit service descriptions. Only assign a category if the service is clearly and directly mentioned in the text. 
Do not infer or assume services based on vague, suggestive, or related language.
Clearly distinguish between primary business types—for example, companies that sell goods made by others (retail/distribution), 
those that manufacture such goods, and those that use them to provide services (e.g., construction or utilities). Be especially 
cautious when assigning subdivided categories under “Computer and Information Technology”.
If there is any ambiguity, or the service is mentioned only indirectly, do not assign the category.
When in doubt, recheck category definitions and default to “Other” if there's not enough explicit evidence.
"""

wikipedia_dev_prompt = """
You are tasked with analyzing the content of a Wikipedia page for a specific organization to classify the organization into one or more business categories.
Carefully review the company information presented on the page. Only assign a category if the service is clearly and directly mentioned in the text. 
Do not infer or assume services based on vague, suggestive, or related language.
Clearly distinguish between primary business types—for example, companies that sell goods made by others (retail/distribution), 
those that manufacture such goods, and those that use them to provide services (e.g., construction or utilities). Be especially 
cautious when assigning subdivided categories under “Computer and Information Technology”.
If there is any ambiguity, or the service is mentioned only indirectly, do not assign the category.
When in doubt, recheck category definitions and default to “Other” if there's not enough explicit evidence.
"""

main_user_prompt_part0 = """
Based on the text from a company's website, determine its business types. 
Choose the appropriate types from the following list and return only the full category names. 
Do not include any additional words.
"""

wikipedia_user_prompt_part0 = """
Based on the text from the Wikipedia page of a company, determine its business types. 
Choose the appropriate types from the following list and return only the full category names. 
Do not include any additional words.
"""

user_prompt_part1 = """
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
"Agriculture, Mining, and Refineries - Farming, Greenhouses, Mining, Forestry, and Animal Farming",
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
"Retail Stores, Wholesale, and E-commerce Sites - Food, Grocery, Beverages",
"Retail Stores, Wholesale, and E-commerce Sites - Clothing, Fashion, Luggage",
"Retail Stores, Wholesale, and E-commerce Sites - Other",
"Manufacturing - Automotive and Transportation",
"Manufacturing - Food, Beverage, and Tobacco",
"Manufacturing - Clothing and Textiles",
"Manufacturing - Machinery",
"Manufacturing - Chemical and Pharmaceutical Manufacturing",
"Manufacturing - Electronics and Computer Components",
"Manufacturing - Other",
"Other - Individually Owned",
"Website issue - Cannot determine categories"
"""

user_prompt_part2 = """
To help you distinguish between similar categories, here are short definitions of some potentially ambiguous ones:
"Other - Individually Owned": "A blog or personal webpage that contains information about a specific individual. Assign this category if the website primarily showcases an individual's personal background, resume, experience, projects, or hobby infrastructure—even if technically advanced."
"Computer and Information Technology - Internet Service Provider (ISP)": "Companies that provide individuals and organizations access to the internet via services like FTTH and FTTO. Entities operating a fiber optic backbone to deliver internet connectivity to regions or to educational and research institutions are also considered ISPs. However, if a company only constructs cellular towers or deploys fiber infrastructure without directly providing internet services, it should not be labeled as an ISP, but instead as 'Construction and Real Estate - Civil Engineering Construction (Utility Lines, Roads, and Bridges).'"
"Computer and Information Technology - Phone Provider": "Only assign this category if the company provides direct connectivity for mobile phones (wireless), fixed-line phones, Voice over IP (VoIP), or Hosted PBX services — using its own telecommunications infrastructure or network access, and do not assign it to companies that offer only interpreting or relay services (e.g., VRS/VRI) or rely entirely on users' existing internet or cellular connections."
"Media, Publishing, and Broadcasting - Radio and Television Providers": "Companies that provide radio and television services, including traditional broadcasting, IPTV, and TV channel offerings."
"Computer and Information Technology - Internet Exchange Point (IXP)": "Entities that operate a physical infrastructure where multiple networks can directly interconnect and exchange internet traffic. Networks or ASes with an open peering policy but without operating such infrastructure are not considered IXPs. If a website only mentions the network's presence at various IXPs for peering—without explicitly stating that it provides IXP services—do not assign this category."
"Computer and Information Technology - Hosting, Cloud Provider, Data Center, Server Colocation": "Companies that provide services such as cloud computing, server hosting (including email, domain, dedicated servers, cloud servers, and VPS hosting), as well as data center and server colocation services. Do not assign this category based solely on mentions of hardware refurbishment, electronics recycling, IT asset disposition, data destruction, or data security practices, unless these are clearly part of a hosting, cloud, or colocation service offering."
"Computer and Information Technology - Other": "Include all computer and information technology-related networks, such as experimental peering networks, IP leasing, and those that do not fit into any other computer-related categories in the above list. For experimental networks maintained by individuals out of personal interest with an open peering policy, use only this category."
"Computer and Information Technology - Technology Consulting Services": "Companies that provide computer technology-related services, such as data analytics, technical support, solutions, and more, for specific business markets."
"Service - Law, Business, and Consulting Services": "Companies that provide services related to law, business, or industry-specific consulting. This category excludes companies offering services in the computer or technology sectors, or those providing tech-related solutions."
"Computer and Information Technology - Software Development": "Companies that primarily develop software products or custom software solutions. This includes web and mobile applications, enterprise software, system integration tools, and more. If a company is labeled as software development, assign additional industry-specific categories only if a particular field is clearly their primary focus. Do not assign other industry-specific categories if their products or services broadly span multiple fields without a dominant focus."
"Retail Stores, Wholesale, and E-commerce Sites - Other": "Companies engaged in the retail, distribution, or sale of goods and equipment for specific industries—such as IT equipment, construction materials, water systems, lighting equipment, and similar. Carefully distinguish companies focused on retailing (this category) from those involved in manufacturing or providing specific services (which should be classified under other appropriate categories)."
"Website issue - Cannot determine categories": "When a website lacks correct or sufficient information (not having enough descriptive text about its services), is under construction or maintenance, or requires login, third-party verification, or human interaction, or default webpage."
"""

perplexity_ai_fallback_prompt = """
Based on the organization's name and the country code of its registered country, search for industrial business-related information and classify the organization into one or more of the following NAICSlite business categories. You should prioritize citations from official or reputable sources (e.g., the organization's website, verified social media pages, or government business directories).

Company Name (country code): {0}

Categories:
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
"Agriculture, Mining, and Refineries - Farming, Greenhouses, Mining, Forestry, and Animal Farming",
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
"Retail Stores, Wholesale, and E-commerce Sites - Food, Grocery, Beverages",
"Retail Stores, Wholesale, and E-commerce Sites - Clothing, Fashion, Luggage",
"Retail Stores, Wholesale, and E-commerce Sites - Other",
"Manufacturing - Automotive and Transportation",
"Manufacturing - Food, Beverage, and Tobacco",
"Manufacturing - Clothing and Textiles",
"Manufacturing - Machinery",
"Manufacturing - Chemical and Pharmaceutical Manufacturing",
"Manufacturing - Electronics and Computer Components",
"Manufacturing - Other",
"Other - Individually Owned",

Please ensure that the cited information corresponds accurately to the queried organization by verifying both the exact organization name and its registered country.

If the organization name appears to be a person's name and no exact matched company information can be found online, respond with:
"Other - Individually Owned"

Do not hallucinate or infer beyond available evidence. Return only categories that are supported by explicitly found descriptions. Do not assign 'Computer and Information Technology - Internet Service Provider (ISP)' solely based on the presence of an ASN. Only assign this category if the source explicitly indicates that the organization provides ISP-related business services. If no high-confidence information is available for the queried organization, respond with:
"Cannot determine categories"

Please ensure that you list the exact resulting categories at the very beginning of your response.
"""