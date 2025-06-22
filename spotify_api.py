import requests
import base64

CLIENT_ID = 'CLIENT_ID'
CLIENT_SECRET = 'CLIENT_SECRET'

# קבלת access token
def get_access_token():
    auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()

    response = requests.post(
        'https://accounts.spotify.com/api/token',
        headers={
            'Authorization': f'Basic {b64_auth}'
        },
        data={
            'grant_type': 'client_credentials'
        }
    )

    if response.status_code != 200:
        raise Exception("Failed to get access token", response.text)

    return response.json()['access_token']

# קבלת פרק ראשון של פודקאסט לפי show_id
def get_first_episode_duration(show_id, token):
    url = f"https://api.spotify.com/v1/shows/{show_id}/episodes?limit=1"
    headers = {'Authorization': f'Bearer {token}'}
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        return None

    items = response.json().get('items', [])
    if not items:
        return None

    # משך הפרק במילישניות
    duration_ms = items[0].get('duration_ms', 0)
    return duration_ms / 60000  # לדקות

# חיפוש פודקאסטים לפי קטגוריה וסינון לפי משך פרק ראשון
def search_podcasts_by_category(category, max_duration_minutes=None, limit=10):
    token = get_access_token()
    headers = {'Authorization': f'Bearer {token}'}

    query = f'{category} podcast'
    response = requests.get(
        'https://api.spotify.com/v1/search',
        headers=headers,
        params={
            'q': query,
            'type': 'show',
            'limit': limit
        }
    )

    if response.status_code != 200:
        raise Exception("Search failed", response.text)

    shows = response.json().get('shows', {}).get('items', [])
    filtered = []

    for show in shows:
        if max_duration_minutes is not None:
            episode_duration = get_first_episode_duration(show['id'], token)
            if episode_duration is None or episode_duration > max_duration_minutes:
                continue

        filtered.append({
            'name': show['name'],
            'publisher': show['publisher'],
            'description': show['description'],
            'url': show['external_urls']['spotify'],
            'language': show.get('languages', [''])[0],
        })

    return filtered

# קבלת פודקאסטים פופולריים (trending) - ללא פרמטר token
def get_popular_podcasts(limit=3):
    token = get_access_token()
    headers = {'Authorization': f'Bearer {token}'}
    
    # נסיון למצוא פודקאסטים פופולריים עם מילות מפתח שונות
    popular_queries = [
        'top podcast israel',
        'popular podcast hebrew',
        'trending podcast',
        'best podcast 2024',
        'פודקאסט ישראל'
    ]
    
    all_podcasts = []
    
    for query in popular_queries:
        response = requests.get(
            'https://api.spotify.com/v1/search',
            headers=headers,
            params={
                'q': query,
                'type': 'show',
                'market': 'IL',  # שוק ישראלי
                'limit': 10
            }
        )

        if response.status_code == 200:
            shows = response.json().get('shows', {}).get('items', [])
            for show in shows:
                # נוסיף רק פודקאסטים שעדיין לא קיימים ברשימה
                if not any(p['name'] == show['name'] for p in all_podcasts):
                    all_podcasts.append({
                        'name': show['name'],
                        'publisher': show['publisher'],
                        'description': show['description'][:300] + "..." if len(show['description']) > 300 else show['description'],
                        'url': show['external_urls']['spotify'],
                        'language': show.get('languages', [''])[0] if show.get('languages') else 'unknown',
                        'total_episodes': show.get('total_episodes', 0)
                    })
    
    # נחזיר את הפודקאסטים הראשונים לפי הכמות המבוקשת
    return all_podcasts[:limit]

# פונקציה חדשה לקבלת פודקאסטים פופולריים בישראל
def get_israeli_popular_podcasts(limit=3):
    token = get_access_token()
    headers = {'Authorization': f'Bearer {token}'}
    
    israeli_queries = [
        'פודקאסט',
        'podcast israel',
        'hebrew podcast',
        'ישראל פודקאסט',
        'עברית פודקאסט'
    ]
    
    all_podcasts = []
    
    for query in israeli_queries:
        response = requests.get(
            'https://api.spotify.com/v1/search',
            headers=headers,
            params={
                'q': query,
                'type': 'show',
                'market': 'IL',
                'limit': 15
            }
        )

        if response.status_code == 200:
            shows = response.json().get('shows', {}).get('items', [])
            for show in shows:
                if not any(p['name'] == show['name'] for p in all_podcasts):
                    all_podcasts.append({
                        'name': show['name'],
                        'publisher': show['publisher'],
                        'description': show['description'][:250] + "..." if len(show['description']) > 250 else show['description'],
                        'url': show['external_urls']['spotify'],
                        'total_episodes': show.get('total_episodes', 0)
                    })
    
    return all_podcasts[:limit]
