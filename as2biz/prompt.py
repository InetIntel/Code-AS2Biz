# classification_instructions = """
# You are tasked with analyzing the content of a given organization's website to classify the organization into one or more business categories.
# Carefully review the website's explicit service descriptions. Only assign a category if the service is clearly and directly mentioned in the text.
# Do not infer or assume services based on vague, suggestive, or related language.
# Clearly distinguish between primary business types-for example, companies that sell goods made by others (retail/distribution),
# those that manufacture such goods, and those that use them to provide services (e.g., construction or utilities). Be especially
# cautious when assigning subdivided categories under "Computer and Information Technology".
# If there is any ambiguity, or the service is mentioned only indirectly, do not assign the category.
# When in doubt, recheck category definitions and default to "Other" if there's not enough explicit evidence.
# Try to be inclusive when assigning labels, especially when a company's primary business is strongly tied to a single sector, as long as the labels are grounded in the website content. For example, if an insurance provider focuses only on the health sector, you should label it as both “Finance - Insurance” and “Health - Other.” However, if it provides services across many sectors (e.g., insurance for multiple industries), only label it using the primary category “Finance - Insurance.”
# Before you give your final answer, double-check all types you plan to include and their corresponding definitions, and remove any types that conflict with their definitions.
# In your final answer, ensure all category names match the given English taxonomy exactly.
# """

classification_instructions = """
You are tasked with analyzing the content of a given organization's website to classify the organization into one or more business categories.
Carefully review the website's explicit service descriptions. Only assign a category if the service is clearly and directly mentioned in the text.
Do not infer or assume services based on vague, suggestive, or related language.

Clearly distinguish between primary business types—for example, companies that sell goods made by others (retail/distribution),
those that manufacture such goods, and those that use them to provide services (e.g., construction or utilities). Be especially
cautious when assigning subdivided categories under "Computer and Information Technology".

Very important: distinguish between organizations that:
(1) directly operate in an industry (for example, a hospital treating patients, a bank holding deposits or issuing loans,
    an insurance carrier underwriting policies), and
(2) mainly provide software, platforms, data, analytics, or informational content to customers in those industries.

If the organization falls into case (2)—for example, it provides software or decision-support tools for healthcare, finance, law, or tax professionals—
you should primarily use the appropriate "Computer and Information Technology" and/or "Media, Publishing, and Broadcasting" categories,
and you should NOT assign the client industries (such as "Health Care Services" or "Finance and Insurance") unless the organization also clearly
operates those services itself.

If there is any ambiguity, or the service is mentioned only indirectly, do not assign the category.
When in doubt, recheck category definitions and default to "Other" if there's not enough explicit evidence.

Try to be inclusive when assigning labels ONLY for organizations that directly provide the underlying services in a single sector.
For example, if an insurance provider directly offers insurance products only in the health sector, you may label it as both
“Finance - Insurance” and “Health - Other.” However, if it provides services across many sectors (e.g., insurance for multiple industries),
only label it using the primary category “Finance - Insurance.”

This inclusiveness rule does NOT apply to software vendors, data providers, content platforms, or technology/consulting firms whose main products
serve many industries. For such companies, prioritize the correct "Computer and Information Technology" and/or "Media, Publishing, and Broadcasting"
categories, and do NOT add extra sector labels like "Health Care Services", "Finance and Insurance", or "Service - Law, Business, and Consulting Services"
unless the company itself actually runs a hospital, bank, insurance carrier, or law firm.

Before you give your final answer, double-check all types you plan to include and their corresponding definitions, and remove any types that conflict with their definitions.
When the page appears to be a non-functional or placeholder site, do not guess a business category. For example, if the content is a default hosting or template page
(such as 'This site is hosted by ...', 'Welcome to your new website', 'Powered by <hosting provider>'), a domain parking or registrar landing page
('This domain is for sale', 'Coming soon'), a bare server/directory index, an error/maintenance page, or a generic login screen with no description of the organization or its services,
you should not infer the business type of the organization. In these cases, return only the special label "Website issue - Cannot determine categories".

In your final answer, ensure all category names match the given English taxonomy exactly.
"""

# Preserve the developer prompt used before the 2026-09 method update.  The
# active prompt below is intentionally versioned in place so older snapshots
# remain reproducible.
classification_instructions_before_202609 = classification_instructions

classification_instructions = """
Classify the organization that owns or operates the supplied website into one or more categories from the provided taxonomy.
The goal is complete coverage of the organization's clearly evidenced, current operations without speculative, incidental, or redundant labels.

Evidence and subject attribution:
- Assign a category only when the website shows that the organization itself, or a clearly controlled business unit or subsidiary, currently provides, operates, manufactures, publishes, sells, or delivers that product or service.
- A service/product navigation item, offering page, or repeated first-party description can be direct evidence. A keyword by itself is not enough.
- Do not classify customers, partners, suppliers, sponsors, portfolio companies, tenants, advertisers, or unrelated organizations mentioned on the site as businesses of the site owner.
- Ignore news topics, case studies, customer stories, job postings, research subjects, event listings, awards, co-branded promotions, and links to third-party services unless they explicitly describe a current operation of the organization itself.
- Treat abrupt off-topic passages, casino/betting promotions on an unrelated site, and other likely SEO injection or scraped-content contamination as noise. Do not let them create labels.
- For a corporate group, include distinct sectors only when the site presents them as operations of the group or its controlled divisions/subsidiaries. Do not infer operations from investments, clients, partnerships, or broad mission language.
- Internal operations are not external business lines. Sustainability/ESG disclosures, regulatory filings or permits, emissions and resource-use data, procurement, internal utilities, and facilities used to run the organization's own business do not support a category unless the site also shows that the organization offers that product or service externally or operates it as a separately described business line.

Precision checks for recurring confusions:
- Owning, occupying, financing, or managing a building does not mean the organization performs building construction. Cabling, installation, studio facilities, campuses, dormitories, and internal facilities also do not by themselves imply construction or real-estate operations.
- Researching a substance, disease, technology, or industry does not mean manufacturing products in that field. Manufacturing requires evidence that the organization makes or produces goods.
- A co-branded credit card, accepted payment method, referral, or financing partner does not make the organization a bank or lender. Assign a finance category only when it directly provides the defined financial service.
- Student housing, residential accommodation, employee housing, campuses, and production facilities are not hotels or traveler accommodation unless lodging is explicitly offered to travelers as a business.
- Physical surveillance, alarms, access control, and CCTV are not Computer and Network Security unless cybersecurity or network/information security is explicitly offered.
- Generic telecommunications, equipment installation, or cabling is not Phone Provider. Phone Provider requires direct fixed-line, mobile, VoIP, or hosted-PBX connectivity/service.
- Satellite Communication applies when the organization directly provides satellite/V-SAT connectivity or communications. Merely installing satellite-TV equipment or mentioning satellites is insufficient.
- Online Music and Video Streaming Services requires an actual audience/customer-facing streaming service or a commercial streaming service offered to clients. IPTV, OTT, catch-up TV, and explicitly offered live/on-demand streaming can qualify. A corporate video hub, branded “TV” page, webinar library, embedded videos, marketing videos, or informational video content alone does not qualify.
- Apply Radio and Television Providers only when the organization directly operates or supplies a television/radio channel, broadcast, or subscription television/radio service. The mere presence of video content does not qualify.
- Web development, web marketing, SEO, and other technology services are not Law, Business, and Consulting Services merely because they help businesses; use the appropriate IT category.
- Computer and Information Technology - Other is a fallback for a genuine IT/network operation not captured by a more specific IT category. Do not add it redundantly for the same evidence when a specific IT category already applies. Use both only when a separate, clearly evidenced IT/network business line remains outside every specific IT category.

Completeness check:
- Do not stop after finding the most obvious or primary label. Make one final pass for every separately described, directly operated business line.
- For universities, hospitals, governments, tribal organizations, and diversified groups, include clearly operated specialized functions such as research institutes, medical laboratories, publishing, training, lending, communications, data centers, utilities, or transport when the text directly supports them.
- Do not add sector labels to a broad government portal merely because it discusses public responsibilities. Add them only for a specialized agency or a genuinely operated, separately evidenced service consistent with the taxonomy definitions.

Website issue handling:
- Use Website issue only for an error, placeholder, parked domain, generic login, default hosting page, or another page that does not credibly identify an organization or any business activity.
- Do not force Website issue merely because a first-party official site is short or sparse. If it credibly identifies the organization and supports a broad taxonomy category, that category may be used.
- If Website issue is selected, return it alone. If at least one business category is supported, do not also select Website issue.

Before answering, internally challenge every candidate for direct evidence and subject attribution, remove redundant fallback categories, then check once for missed explicit operations.
Return only the selected category IDs. Use only IDs shown with the taxonomy; never invent an ID or rewrite a category name. The API response schema specifies the exact output structure.
"""

classification_wiki_instructions = """
You are tasked with analyzing the content of a Wikipedia page for a specific organization to classify the organization into one or more business categories.
Carefully review the company information presented on the page. Only assign a category if the service is clearly and directly mentioned in the text.
Do not infer or assume services based on vague, suggestive, or related language.
Clearly distinguish between primary business types-for example, companies that sell goods made by others (retail/distribution),
those that manufacture such goods, and those that use them to provide services (e.g., construction or utilities). Be especially
cautious when assigning subdivided categories under "Computer and Information Technology".
If there is any ambiguity, or the service is mentioned only indirectly, do not assign the category.
When in doubt, recheck category definitions and default to "Other" if there's not enough explicit evidence.
"""

template_singlemodal = """
Based on the text from a company's website, determine its business types.
Choose the appropriate types from the following list and return only the full category names.
Do not include any additional words.
"""

template_singlemodal_wiki = """
Based on the text from the Wikipedia page of a company, determine its business types.
Choose the appropriate types from the following list and return only the full category names.
Do not include any additional words.
"""

taxonomy = """
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
"""

taxonomy_list = ["Computer and Information Technology - Internet Service Provider (ISP)",
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
"Other - Individually Owned"]

# Versioned category IDs freeze the model-output contract for the 2026-09
# website-classification prompt.  Append new categories rather than renumbering
# existing IDs.
category_id_to_name_202609 = {
    f"C{index:03d}": name for index, name in enumerate(taxonomy_list, start=1)
}
category_id_to_name_202609["C097"] = "Website issue - Cannot determine categories"
category_name_to_id_202609 = {
    name: category_id for category_id, name in category_id_to_name_202609.items()
}

assert len(category_id_to_name_202609) == 97
assert len(category_name_to_id_202609) == 97

template_singlemodal_202609 = """
Based on the text from a company's website, determine its business types.
Choose the appropriate types from the following list using the category IDs shown.
Return category IDs only; the response schema supplies the output structure.
"""

taxonomy_with_ids_202609 = ",\n".join(
    f'"[{category_id}] {name}"'
    for category_id, name in category_id_to_name_202609.items()
    if category_id != "C097"
)

# descr = """
# To help you distinguish between similar categories, here are short definitions of some potentially ambiguous ones:
# "Other - Individually Owned": "A blog or personal webpage that contains information about a specific individual. Assign this category if the website primarily showcases an individual's personal background, resume, experience, projects, or hobby infrastructure - even if technically advanced."
# "Computer and Information Technology - Internet Service Provider (ISP)": "Companies that provide individuals and organizations access to the internet via services like FTTH and FTTO or other types of Internet connectivity such as leased line, darkfiber, and DIA. Entities operating a fiber optic backbone to deliver internet connectivity to regions or to educational and research institutions are also considered ISPs. However, if a company only constructs cellular towers or deploys fiber infrastructure without directly providing internet services, it should not be labeled as an ISP, but instead as 'Construction and Real Estate - Civil Engineering Construction (Utility Lines, Roads, and Bridges).'"
# "Computer and Information Technology - Phone Provider": "Only assign this category if the company provides direct connectivity for mobile phones (wireless), fixed-line phones, Voice over IP (VoIP), or Hosted PBX services - using its own telecommunications infrastructure or network access, and do not assign it to companies that offer only interpreting or relay services (e.g., VRS/VRI). Additionally, if the company provides mobile/cellular Internet data services, also label it as 'Computer and Information Technology - Internet Service Provider (ISP)'."
# "Media, Publishing, and Broadcasting - Radio and Television Providers": "Companies that provide radio and television services, including traditional broadcasting, IPTV, and TV channel offerings. An ISP may also provide this type of service."
# "Media, Publishing, and Broadcasting - Online Music and Video Streaming Services": "Companies that provide online streaming services. An ISP may also provide this type of service."
# "Computer and Information Technology - Internet Exchange Point (IXP)": "Entities that operate a physical infrastructure where multiple networks can directly interconnect and exchange internet traffic. Networks or ASes with an open peering policy but without operating such infrastructure are not considered IXPs. If a website only mentions the network's presence at various IXPs for peering-without explicitly stating that it provides IXP services-do not assign this category."
# "Computer and Information Technology - Hosting, Cloud Provider, Data Center, Server Colocation": "Companies that provide services such as cloud computing, server hosting (including email, domain, dedicated servers, cloud servers, and VPS hosting), as well as data center and server colocation services. Do not assign this category based solely on mentions of hardware refurbishment, electronics recycling, IT asset disposition, data destruction, or data security practices, unless these are clearly part of a hosting, cloud, or colocation service offering."
# "Computer and Information Technology - Other": "Include all computer and information technology-related networks, such as experimental peering networks, IP leasing, and those that do not fit into any other computer-related categories in the above list. For experimental networks maintained by individuals out of personal interest with an open peering policy, use only this category. If the company fits a more specific Computer and Information Technology category, do not apply this one."
# "Computer and Information Technology - Software Development": "Companies that not only help other companies develop software but also have at least one software product of their own. If a company is labeled as software development, assign additional industry-specific categories only if a particular field is clearly their primary focus. Do not assign other industry-specific categories if their products or services broadly span multiple fields without a dominant focus."
# "Computer and Information Technology - Technology Consulting Services": "Companies that provide computer technology-related services or solutions, such as data analytics, technical support, solutions for certain fields, and more, for specific business markets. If the company provides only network solutions but not explicitly mention that they provide Internet access, do not label it as 'Computer and Information Technology - Internet Service Provider (ISP)'."
# "Service - Law, Business, and Consulting Services": "Companies that provide services related to law, business, or industry-specific consulting. This category excludes companies offering services in the computer or technology sectors, or those providing tech-related solutions. If you have already applied the Tech Consulting label, do not apply this category again."
# "Retail Stores, Wholesale, and E-commerce Sites - Other": "Companies engaged in the retail, distribution, or sale of goods and equipment for specific industries-such as IT equipment, construction materials, water systems, lighting equipment, and similar. Carefully distinguish companies focused on retailing (this category) from those involved in manufacturing or providing specific services (which should be classified under other appropriate categories)."
# "Website issue - Cannot determine categories": "When a website lacks correct or sufficient information (not having enough descriptive text about its services), is under construction or maintenance, or requires login, third-party verification, or human interaction, or default webpage."
# """
descr = """
To help you distinguish between similar categories, here are short definitions of some potentially ambiguous ones:

"Other - Individually Owned": "A blog or personal webpage that contains information about a specific individual. Assign this category if the website primarily showcases an individual's personal background, resume, experience, projects, or hobby infrastructure - even if technically advanced."

"Computer and Information Technology - Internet Service Provider (ISP)": "Companies that provide individuals and organizations access to the internet via services like FTTH and FTTO or other types of Internet connectivity such as leased line, darkfiber, and DIA. Entities operating a fiber optic backbone to deliver internet connectivity to regions or to educational and research institutions are also considered ISPs. However, if a company only constructs cellular towers or deploys fiber infrastructure without directly providing internet services, it should not be labeled as an ISP, but instead as 'Construction and Real Estate - Civil Engineering Construction (Utility Lines, Roads, and Bridges)."

"Computer and Information Technology - Phone Provider": "Only assign this category if the company provides direct connectivity for mobile phones (wireless), fixed-line phones, Voice over IP (VoIP), or Hosted PBX services - using its own telecommunications infrastructure or network access, and do not assign it to companies that offer only interpreting or relay services (e.g., VRS/VRI). Additionally, if the company provides mobile/cellular Internet data services, also label it as 'Computer and Information Technology - Internet Service Provider (ISP)'."

"Media, Publishing, and Broadcasting - Radio and Television Providers": "Companies that provide radio and television services, including traditional broadcasting, IPTV, and TV channel offerings. An ISP may also provide this type of service."

"Media, Publishing, and Broadcasting - Online Music and Video Streaming Services": "Companies that provide online streaming services. An ISP may also provide this type of service."

"Computer and Information Technology - Internet Exchange Point (IXP)": "Entities that operate a physical infrastructure where multiple independent networks can directly interconnect and exchange Internet traffic. An IXP typically runs one or more switching fabrics and offers ports to many member networks, often describing 'participants/members', 'joining the exchange', 'route servers', 'port fees', or 'peering LANs'. You should only assign this category if the organization clearly states that it OPERATES an Internet Exchange or Internet Exchange Point itself. A network that simply lists the IXPs it is connected to, or says that it 'peers over IX' or 'is present at' certain IXPs, is NOT an IXP; such networks are just participants and should be classified instead as ISPs, hosting providers, or 'Computer and Information Technology - Other'. For example, if a network lists upstream providers, shows 'Peering Available: Yes, Peering over IX', and lists several IXPs where it peers, but never claims to operate an exchange facility for other networks, do NOT label it as an IXP."

"Computer and Information Technology - Hosting, Cloud Provider, Data Center, Server Colocation": "Companies that provide services such as cloud computing, server hosting (including email, domain, dedicated servers, cloud servers, and VPS hosting), as well as data center and server colocation services. Do not assign this category based solely on mentions of hardware refurbishment, electronics recycling, IT asset disposition, data destruction, or data security practices, unless these are clearly part of a hosting, cloud, or colocation service offering."

"Computer and Information Technology - Other": "Include all computer and information technology-related networks that do not fit into any other computer-related categories in the above list. This includes experimental, hobby, or personal networks maintained by individuals, open peering networks that list upstreams and IXPs where they connect but do not themselves operate an Internet Exchange Point, IP leasing or tunneling networks, and similar cases. For such networks, even if they mention 'peering over IX' or list multiple IXPs where they are present, do NOT label them as an IXP unless the website clearly states they operate an exchange facility for other networks. If the company fits a more specific Computer and Information Technology category, do not apply this one."

"Computer and Information Technology - Software Development": "Companies that not only help other companies develop software but also have at least one software product or software-based service of their own (including SaaS platforms, cloud services, or proprietary applications) that is offered to multiple customers. Typical signs include named products or platforms, feature pages, pricing or subscription information, login portals, or demo/free trial flows. If a company is labeled as software development, assign additional industry-specific categories only if a particular field is clearly their primary focus AND they directly operate that type of service (for example, a bank that develops its own online banking platform). If their products or services broadly span multiple fields (for example, software and content for healthcare, finance, legal, and tax professionals) and they do NOT themselves operate hospitals, banks, or law firms, do NOT assign those industry-specific categories; keep only the appropriate Computer and Information Technology and/or Media categories."

"Computer and Information Technology - Technology Consulting Services": "Companies whose main business is providing technology-related services or projects to clients, such as AI and data analytics consulting, system integration, custom software development for individual clients, IT outsourcing, managed services, implementation of third-party platforms, or technical advisory services. Typical signs include repeated references to 'consulting', 'professional services', 'projects', 'engagements', 'tailored solutions', or 'we help clients adopt or implement technology'. If the company only offers consulting and project-based services without clearly offering a reusable software product or SaaS platform of its own, use only this category (and do not label it as Software Development). If the company clearly markets both its own software product/platform and substantial consulting or implementation services around it, you may assign both 'Computer and Information Technology - Software Development' and 'Computer and Information Technology - Technology Consulting Services'."

"Service - Law, Business, and Consulting Services": "Companies that provide services related to law, business, or industry-specific consulting. This category excludes companies offering services in the computer or technology sectors, or those providing tech-related solutions. If you have already applied the Tech Consulting label, do not apply this category again."

"Retail Stores, Wholesale, and E-commerce Sites - Other": "Companies engaged in the retail, distribution, or sale of goods and equipment for specific industries—such as IT equipment, construction materials, water systems, lighting equipment, and similar. Carefully distinguish companies focused on retailing (this category) from those involved in manufacturing or providing specific services (which should be classified under other appropriate categories)."

"Health Care Services - Other": "Organizations that directly deliver or coordinate medical or health-related services to patients or residents (for example, hospitals, clinics, medical practices, nursing homes, or home health care providers). Do NOT assign this category to companies that only provide software, data, analytics, or informational content for healthcare professionals."

"Finance and Insurance - Banks, Credit Card Companies, Mortgage Providers": "Organizations that directly provide financial services such as accepting deposits, issuing loans or credit, operating credit cards, or offering mortgage products. Do NOT assign this category to companies that only provide financial software, tax software, accounting tools, or financial information."

"Finance and Insurance - Insurance Carriers and Agencies": "Organizations that directly underwrite or sell insurance policies. Do NOT assign this category to companies that only provide software, analytics, or information for the insurance industry."

"Government and Public Administration - Government and Regulatory Agencies, Administrations, Departments, and Services": "General-purpose government entities at any level (city, municipality, county, regional, state, national) and their main portals or departments that provide or coordinate many different public services, such as public safety, permits, taxes, parks, utilities, public works, and community services. For these broad government portals, do NOT add separate sector labels like Utilities, Health Care, or Education solely because they describe or manage those services; such websites should be classified only under this Government category unless the site clearly represents a specialized agency whose primary mission is limited to a single sector (for example, a dedicated water district or public hospital)."

"Website issue - Cannot determine categories": "Use for an error, placeholder, parked domain, generic login, default hosting page, or another page that does not credibly identify an organization or any business activity. Do not use it merely because a first-party official website is short or sparse; a supported broad category may still be assigned."
"""

descr_with_category_ids_202609 = descr.replace(
    '"Website issue - Cannot determine categories"',
    '"[C097] Website issue - Cannot determine categories"',
    1,
)
assert descr_with_category_ids_202609 != descr

## Fallback web-search classification (last resort, per-organization)
#
# Used by as2biz/fallback_openai_search.py for ASNs still unclassified after the
# website, sibling-inheritance, and Wikipedia passes. The model (2026-03: OpenAI
# gpt-5.2 + the built-in web_search tool) is given only the organisation name and
# country code and must return structured JSON.
#
# The full prompt sent is:  descr + "\n\n" + fallback_web_search_prompt
# (`descr` above is the shared category-disambiguation text.)

fallback_web_search_prompt = """
Based on the organization's name and the country code of its registered country, search for industrial business-related information and classify the organization into one or more of the following NAICSlite business categories. You should prioritize citations from official or reputable sources (e.g., the organization's website, verified social media pages, or government business directories).

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
"Other - Individually Owned",

Match the queried organization exactly:
- The cited information must describe this specific organization in the country given by the country code, not a same-named organization elsewhere. Use the country code to disambiguate.
- Accept information about a different-country entity only if the page clearly belongs to a multinational company, or its parent/group, whose stated operating footprint includes the queried country.
- If the only information you can find is about a same-named organization in a different country, with no evidence its footprint covers the queried country, set "cannot_determine" to true.
- If two or more distinct organizations plausibly match and you cannot confidently identify the right one, set "cannot_determine" to true.

Do not classify from these sources:
- Third-party internet-registry, whois, BGP/ASN-lookup, or network-info aggregator pages (for example PeeringDB, bgp.he.net, bgp.tools, RIPE, ARIN, APNIC, LACNIC, AFRINIC, ipinfo.io). These describe the network, not the organization's business, and must not be used to assign any category. If that is all you can find, set "cannot_determine" to true.
- The mere existence of an ASN, an IP allocation, or a network presence. Do not assign 'Computer and Information Technology - Internet Service Provider (ISP)', 'IP Transit', 'Hosting, Cloud Provider, Data Center, Server Colocation', or any other IT category on that basis. Assign an IT category only when a source explicitly describes the organization selling or operating that IT service as a business.

Subject and evidence:
- Assign a category only when a source shows that the organization itself, or a clearly controlled subsidiary or division, currently provides, operates, manufactures, publishes, or sells that product or service.
- Do not classify the organization's customers, partners, suppliers, sponsors, investees, tenants, or clients as its own business lines. Ignore news topics, case studies, job postings, research subjects, event listings, and awards.

Precision checks for recurring confusions:
- Owning, occupying, financing, or managing a building does not mean the organization performs building construction. Cabling, installation, studio facilities, campuses, dormitories, and internal facilities also do not by themselves imply construction or real-estate operations.
- Researching a substance, disease, technology, or industry does not mean manufacturing products in that field. Manufacturing requires evidence that the organization makes or produces goods.
- A co-branded credit card, accepted payment method, referral, or financing partner does not make the organization a bank or lender. Assign a finance category only when it directly provides the defined financial service.
- Student housing, residential accommodation, employee housing, campuses, and production facilities are not hotels or traveler accommodation unless lodging is explicitly offered to travelers as a business.
- Physical surveillance, alarms, access control, and CCTV are not Computer and Network Security unless cybersecurity or network/information security is explicitly offered.
- Generic telecommunications, equipment installation, or cabling is not Phone Provider. Phone Provider requires direct fixed-line, mobile, VoIP, or hosted-PBX connectivity/service.
- Satellite Communication applies when the organization directly provides satellite/V-SAT connectivity or communications. Merely installing satellite-TV equipment or mentioning satellites is insufficient.
- Online Music and Video Streaming Services requires an actual audience/customer-facing streaming service or a commercial streaming service offered to clients. IPTV, OTT, catch-up TV, and explicitly offered live/on-demand streaming can qualify. A corporate video hub, branded "TV" page, webinar library, embedded videos, marketing videos, or informational video content alone does not qualify.
- Apply Radio and Television Providers only when the organization directly operates or supplies a television/radio channel, broadcast, or subscription television/radio service. The mere presence of video content does not qualify.
- Web development, web marketing, SEO, and other technology services are not Law, Business, and Consulting Services merely because they help businesses; use the appropriate IT category.
- Do not add sector labels to a broad government portal merely because it discusses public responsibilities. Add them only for a specialized agency or a genuinely operated, separately evidenced service consistent with the taxonomy definitions.
- Computer and Information Technology - Other is a fallback for a genuine IT/network operation not captured by a more specific IT category. Do not add it redundantly for the same evidence when a specific IT category already applies.
- Do not add any other '... - Other' category redundantly when a specific category in the same group already covers the same evidence.

If the organization name appears to be a person's name and no exact-matched company information can be found online, respond with:
"Other - Individually Owned"

Do not hallucinate or infer beyond available evidence. Return only categories supported by explicitly found, first-party or reputable descriptions.
If no high-confidence, correctly-matched information is available for the queried organization, set "cannot_determine" to true and return an empty categories list.

Return a JSON object matching the provided schema.
Set "cannot_determine" to true only when no high-confidence, correctly-matched information is available.
If "cannot_determine" is true, return an empty categories list.
Only use category names exactly as provided in the taxonomy.
Do not include any explanation outside the JSON object.

Organization to classify — Company Name (country code): {0}
"""
