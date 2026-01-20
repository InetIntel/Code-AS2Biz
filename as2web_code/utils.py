# utils.py

from fuzzywuzzy import fuzz

def find_relevant_domain(as_name, org_name, domains):
    as_name = as_name.lower()
    org_name = org_name.lower()
    
    scores = []
    for domain in domains:
        score = 0.5 * fuzz.partial_ratio(domain, as_name) + 0.5 * fuzz.partial_ratio(domain, org_name)
        scores.append((domain, score))
    return sorted(scores, key=lambda x: x[1], reverse=True)[0][0] if scores else None


domain_filter_list = ['ukr.net', 'abv.bg', 'hotmail.com.br', 'inbox.ru', 'live.com', 'vnnic.vn', 
                      'mail.ru', 'foxmail.com', 'icloud.com', 'list.ru', 'mail.com', 'irinn.in', 
                      'meta.ua', 'protonmail.com', 'outlook.com.br', 'qq.com', 'cnic.cn', 'yahoo.in', 
                      'idnic.net', '163.com', '21cn.com', '189.cn', 'nic.ad.jp', 'yahoo.com', 
                      'apnic.net', 'hotmail.com', 'outlook.fr', 'yahoo.co.in', 'me.com', 'hanmail.net', 
                      'rambler.ru', 'hotmail.com.ar', 'yeah.net', 'supplied.unknown', 'yahoo.com.br', 
                      'protonmail.ch', 'email.ua', 'bk.ru', 'proton.me', 'yahoo.com.ar', 'mail.bg', 
                      'outlook.com.ar', 'cnnic.cn', 'bracmail.net', 'mhs.attmail.com', 'yandex.ru', 
                      'bol.com.br', 'pm.me', 'twnic.net.tw', 'ua.fm', 'live.com.ar', 'vip.qq.com', 
                      '126.com', 'twnic.tw', '139.com', 'i.ua', 'nic.or.kr', 'outlook.com', 
                      'rediffmail.com', 'wp.pl', 'gmail.com', "corp.mail.ru", "yahoo.es"]
