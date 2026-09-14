BUILTIN_TEMPLATES = [
    # ── People Search ──
    {"category": "People Search", "name": "LinkedIn Profile", "template": 'site:linkedin.com/in "{name}"', "description": "Search for a person on LinkedIn"},
    {"category": "People Search", "name": "LinkedIn + Company", "template": 'site:linkedin.com/in "{name}" "{company}"', "description": "LinkedIn profile with company filter"},
    {"category": "People Search", "name": "Facebook Profile", "template": 'site:facebook.com "{name}"', "description": "Find Facebook profiles"},
    {"category": "People Search", "name": "Instagram Profile", "template": 'site:instagram.com "{username}"', "description": "Find Instagram accounts"},
    {"category": "People Search", "name": "Twitter/X Profile", "template": 'site:twitter.com "{username}"', "description": "Find Twitter/X accounts"},
    {"category": "People Search", "name": "Reddit User", "template": 'site:reddit.com/user "{username}"', "description": "Find Reddit user profile"},
    {"category": "People Search", "name": "GitHub Profile", "template": 'site:github.com "{username}"', "description": "Find GitHub accounts"},
    {"category": "People Search", "name": "Full Name Search", "template": '"{first_name} {last_name}" -site:linkedin.com', "description": "Search full name excluding LinkedIn"},
    {"category": "People Search", "name": "Name + Location", "template": '"{name}" "{city}" -site:linkedin.com', "description": "Find person in a specific location"},
    {"category": "People Search", "name": "Name + Phone", "template": '"{name}" "{phone}"', "description": "Find pages linking name and phone number"},

    # ── Email / Username ──
    {"category": "Email/Username", "name": "Email Address", "template": '"{email}"', "description": "Search for an email address across the web"},
    {"category": "Email/Username", "name": "Email + Name", "template": '"{email}" "{name}"', "description": "Correlate email with a name"},
    {"category": "Email/Username", "name": "Username Search", "template": '"{username}" -site:twitter.com -site:instagram.com', "description": "Find username mentions beyond major platforms"},
    {"category": "Email/Username", "name": "Email in Pastebin", "template": 'site:pastebin.com "{email}"', "description": "Check if email appears in pastes"},
    {"category": "Email/Username", "name": "Email in GitHub", "template": 'site:github.com "{email}"', "description": "Find email in GitHub repos or commits"},
    {"category": "Email/Username", "name": "Username in GitHub", "template": 'site:github.com "{username}"', "description": "Find GitHub username"},
    {"category": "Email/Username", "name": "Gmail Accounts", "template": '"{target}" "@gmail.com"', "description": "Search for Gmail accounts related to target"},

    # ── File Types ──
    {"category": "File Types", "name": "PDF Documents", "template": 'site:{domain} filetype:pdf "{keyword}"', "description": "Find PDFs on a domain with keyword"},
    {"category": "File Types", "name": "Excel Files", "template": 'site:{domain} filetype:xlsx OR filetype:xls', "description": "Find Excel spreadsheets on a domain"},
    {"category": "File Types", "name": "Word Documents", "template": 'site:{domain} filetype:docx OR filetype:doc', "description": "Find Word documents on a domain"},
    {"category": "File Types", "name": "SQL Dumps", "template": 'filetype:sql "{keyword}"', "description": "Find SQL dump files mentioning keyword"},
    {"category": "File Types", "name": "Config Files", "template": 'site:{domain} filetype:conf OR filetype:cfg OR filetype:ini', "description": "Find configuration files"},
    {"category": "File Types", "name": "Log Files", "template": 'site:{domain} filetype:log', "description": "Find exposed log files"},
    {"category": "File Types", "name": "Backup Files", "template": 'site:{domain} filetype:bak OR filetype:backup OR filetype:old', "description": "Find backup files"},
    {"category": "File Types", "name": "Environment Files", "template": 'site:{domain} filetype:env OR inurl:.env', "description": "Find exposed .env files"},

    # ── Login Pages ──
    {"category": "Login Pages", "name": "Admin Panels", "template": 'site:{domain} inurl:admin OR inurl:administrator OR inurl:login', "description": "Find admin/login pages"},
    {"category": "Login Pages", "name": "WordPress Login", "template": 'site:{domain} inurl:wp-login OR inurl:wp-admin', "description": "Find WordPress login pages"},
    {"category": "Login Pages", "name": "cPanel Login", "template": 'inurl:2082 OR inurl:2083 site:{domain}', "description": "Find cPanel login pages"},
    {"category": "Login Pages", "name": "phpMyAdmin", "template": 'inurl:phpmyadmin site:{domain}', "description": "Find exposed phpMyAdmin instances"},
    {"category": "Login Pages", "name": "Exposed Dashboards", "template": 'site:{domain} inurl:dashboard OR inurl:panel OR inurl:portal', "description": "Find dashboard/portal pages"},

    # ── Cameras / IoT ──
    {"category": "Cameras/IoT", "name": "Open Webcams (generic)", "template": 'inurl:"/view/index.shtml"', "description": "Find open Axis webcams"},
    {"category": "Cameras/IoT", "name": "IP Camera Streams", "template": 'inurl:"/mjpg/video.mjpg"', "description": "Find MJPEG IP camera streams"},
    {"category": "Cameras/IoT", "name": "Hikvision Cameras", "template": 'inurl:"/doc/page/login.asp"', "description": "Find Hikvision camera login pages"},
    {"category": "Cameras/IoT", "name": "Network Video Recorder", "template": 'intitle:"Network Video Recorder" inurl:login', "description": "Find open NVR login pages"},
    {"category": "Cameras/IoT", "name": "Router Admin Pages", "template": 'intitle:"router" inurl:admin OR inurl:setup site:{ip_range}', "description": "Find router admin pages"},
    {"category": "Cameras/IoT", "name": "Shodan Alternative", "template": 'intitle:"index of" inurl:{device_type}', "description": "Find exposed device indices"},

    # ── Subdomains ──
    {"category": "Subdomains", "name": "All Subdomains", "template": 'site:{domain} -www', "description": "Find subdomains of a domain"},
    {"category": "Subdomains", "name": "Dev/Staging", "template": 'site:{domain} inurl:dev OR inurl:staging OR inurl:test OR inurl:beta', "description": "Find dev/staging subdomains"},
    {"category": "Subdomains", "name": "API Endpoints", "template": 'site:{domain} inurl:api OR inurl:v1 OR inurl:v2', "description": "Find API endpoints"},
    {"category": "Subdomains", "name": "Mail Servers", "template": 'site:{domain} inurl:mail OR inurl:webmail OR inurl:smtp', "description": "Find mail-related subdomains"},

    # ── Social Media ──
    {"category": "Social Media", "name": "Cached Twitter Bios", "template": 'cache:twitter.com/{username}', "description": "View cached Twitter profile"},
    {"category": "Social Media", "name": "TikTok Profile", "template": 'site:tiktok.com "@{username}"', "description": "Find TikTok profile"},
    {"category": "Social Media", "name": "Discord Invites", "template": 'site:discord.gg "{server_name}"', "description": "Find Discord server invites"},
    {"category": "Social Media", "name": "Telegram Channels", "template": 'site:t.me "{keyword}"', "description": "Find Telegram channels/groups"},
    {"category": "Social Media", "name": "YouTube Channel", "template": 'site:youtube.com "{channel_name}"', "description": "Find YouTube channel"},
    {"category": "Social Media", "name": "Pinterest Profile", "template": 'site:pinterest.com "{username}"', "description": "Find Pinterest profile"},

    # ── Passwords / Credentials ──
    {"category": "Passwords/Credentials", "name": "Pastebin Dumps", "template": 'site:pastebin.com "{keyword}" password OR passwd', "description": "Find credential dumps on Pastebin"},
    {"category": "Passwords/Credentials", "name": "GitHub Secrets", "template": 'site:github.com "{keyword}" password OR secret OR api_key', "description": "Find secrets leaked in GitHub"},
    {"category": "Passwords/Credentials", "name": "Exposed API Keys", "template": '"{service}" api_key OR apikey OR api_secret filetype:env OR filetype:json', "description": "Find exposed API keys"},
    {"category": "Passwords/Credentials", "name": "AWS Keys", "template": '"AKIA" site:github.com OR site:pastebin.com', "description": "Search for leaked AWS access key IDs"},
    {"category": "Passwords/Credentials", "name": "DB Connection Strings", "template": '"mysql://" OR "postgres://" OR "mongodb://" site:{domain}', "description": "Find exposed database connection strings"},
]
