import csv
import random
import requests
import pandas as pd
import base64
import re
import json
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
import openai
import asyncio
from difflib import SequenceMatcher

# Configuration
OPENAI_API_KEY = 'OPENAI_API_KEY'
TELEGRAM_TOKEN = "TELEGRAM_TOKEN"
CLIENT_ID = 'CLIENT_ID'
CLIENT_SECRET = 'CLIENT_SECRET'
DATASET_CSV = 'podcast_dataset.csv'

# Initialize OpenAI
openai.api_key = OPENAI_API_KEY

class SpotifyAPI:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_expiry = None

    def get_access_token(self):
        if self.token and self.token_expiry and datetime.now() < self.token_expiry:
            return self.token
        
        try:
            auth_str = f"{self.client_id}:{self.client_secret}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()
            response = requests.post(
                'https://accounts.spotify.com/api/token',
                headers={'Authorization': f'Basic {b64_auth}'},
                data={'grant_type': 'client_credentials'},
                timeout=10
            )
            data = response.json()
            self.token = data['access_token']
            self.token_expiry = datetime.now() + timedelta(seconds=data.get('expires_in', 3600))
            return self.token
        except Exception as e:
            print(f"Error getting Spotify token: {e}")
            return None

    def search_podcasts(self, query, max_duration=None, limit=10, language_preference=None):
        """חיפוש פודקאסטים עם העדפת שפה"""
        if not self.get_access_token():
            return []
            
        headers = {'Authorization': f'Bearer {self.token}'}
        
        # בניית שאילתת חיפוש על בסיס העדפת השפה
        if language_preference == 'hebrew':
            search_queries = [
                f'{query} פודקאסט',
                f'פודקאסט {query}',
                f'{query} podcast hebrew',
                f'{query} עברית'
            ]
        elif language_preference == 'english':
            search_queries = [
                f'{query} english podcast -hebrew -עברית',
                f'english {query} podcast USA',
                f'{query} podcast UK',
                f'{query} podcast american',
                f'{query} english language'
            ]
        else:
            search_queries = [f'{query} podcast', f'{query} פודקאסט']
        
        all_results = []
        
        for search_query in search_queries:
            try:
                market = 'US' if language_preference == 'english' else 'IL'
                
                response = requests.get(
                    'https://api.spotify.com/v1/search',
                    headers=headers,
                    params={
                        'q': search_query,
                        'type': 'show',
                        'limit': limit * 2,
                        'market': market
                    },
                    timeout=10
                )
                
                if response.status_code != 200:
                    continue
                    
                shows = response.json().get('shows', {}).get('items', [])
                
                for show in shows:
                    # בדיקת שפה
                    languages = show.get('languages', [])
                    name = show.get('name', '').lower()
                    description = show.get('description', '').lower()
                    
                    # סינון לפי העדפת שפה
                    if language_preference == 'hebrew':
                        if not ('he' in languages or 'iw' in languages or 
                               any(hebrew_word in name for hebrew_word in ['עברית', 'ישראל', 'פודקאסט']) or
                               any(hebrew_word in description for hebrew_word in ['עברית', 'ישראל'])):
                            if 'en' in languages and not any(hebrew_char in name + description for hebrew_char in 'אבגדהוזחטיכלמנסעפצקרשת'):
                                continue
                    
                    elif language_preference == 'english':
                        has_hebrew = any(hebrew_char in name + description for hebrew_char in 'אבגדהוזחטיכלמנסעפצקרשת')
                        
                        if has_hebrew or 'he' in languages or 'iw' in languages:
                            continue
                        
                        if not ('en' in languages or 'en-US' in languages or 'en-GB' in languages):
                            if not languages:
                                if has_hebrew:
                                    continue
                            else:
                                continue
                    
                    # בדיקת משך אם נדרש
                    duration_minutes = self.get_episode_duration(show['id'])
                    if max_duration and duration_minutes and duration_minutes > max_duration:
                        continue
                    
                    # הוספה לתוצאות אם לא קיים כבר
                    if not any(r['name'] == show['name'] for r in all_results):
                        all_results.append({
                            'name': show['name'],
                            'publisher': show['publisher'],
                            'description': show['description'][:200] + "..." if len(show['description']) > 200 else show['description'],
                            'url': show['external_urls']['spotify'],
                            'duration_minutes': duration_minutes,
                            'languages': languages,
                            'total_episodes': show.get('total_episodes', 0)
                        })
                        
            except Exception as e:
                print(f"Error searching Spotify: {e}")
                continue
        
        return all_results[:limit]

    def get_episode_duration(self, show_id):
        """קבלת משך הפרק האחרון"""
        headers = {'Authorization': f'Bearer {self.token}'}
        try:
            response = requests.get(
                f"https://api.spotify.com/v1/shows/{show_id}/episodes?limit=1",
                headers=headers,
                timeout=10
            )
            if response.status_code == 200:
                items = response.json().get('items', [])
                if items:
                    duration_ms = items[0].get('duration_ms', 0)
                    return round(duration_ms / 60000, 1) if duration_ms > 0 else None
        except:
            pass
        return None

class GPTAnalyzer:
    def __init__(self):
        self.conversation_history = {}
    
    async def check_podcast_relevance(self, user_text):
        """בדיקה אם הבקשה רלוונטית לפודקאסטים - בדיקה ידנית קשיחה קודם"""
        
        # בדיקה ידנית קשיחה קודם כל
        manual_check = self.strict_manual_check(user_text)
        if manual_check["confidence"] > 0.8:
            print(f"🔍 Manual strict check for '{user_text}': {manual_check}")
            return manual_check
        
        # רק אם הבדיקה הידנית לא ודאית, ננסה GPT
        system_prompt = """אתה מסווג הודעות של משתמשים. 
        
        הצ'טבוט שלנו מיועד להמליץ על פודקאסטים בלבד.
        
        החזר JSON עם השדה הבא:
        {
            "is_podcast_related": true/false,
            "confidence": 0.0-1.0,
            "reason": "הסבר קצר למה זה קשור או לא קשור לפודקאסטים"
        }
        
        דוגמאות לבקשות שקשורות לפודקאסטים (is_podcast_related: true):
        - "רוצה פודקאסט על ספורט"
        - "משהו לשמוע על טכנולוגיה"
        - "תכנית רדיו על חדשות"
        - "איזה תוכן אודיו על בישול"
        - "שיחות על פסיכולוגיה"
        - "המלצה לתוכנית על עסקים"
        
        דוגמאות לבקשות שלא קשורות לפודקאסטים (is_podcast_related: false):
        - "מה מזג האוויר?"
        - "איך קוראים לך?"
        - "מתי הפסחא?"
        - "היי"
        - "תודה"
        - "מה השעה?"
        - "איך מגיעים לתל אביב?"
        
        חשוב: גם אם המשתמש כותב נושא כללי כמו "ספורט" או "טכנולוגיה" - אם זה יכול להיות בקשה לפודקאסט, החזר true."""

        try:
            response = await openai.ChatCompletion.acreate(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"בדוק אם הבקשה הזו קשורה לפודקאסטים: '{user_text}'"}
                ],
                temperature=0.1,
                max_tokens=200
            )
            
            result = json.loads(response.choices[0].message.content)
            print(f"🔍 GPT check for '{user_text}': {result}")
            return result
            
        except Exception as e:
            print(f"Error in podcast relevance check: {e}")
            return manual_check
    
    def strict_manual_check(self, user_text):
        """בדיקה ידנית קשיחה מאוד לבקשות שלא קשורות לפודקאסטים"""
        text_lower = user_text.lower().strip()
        
        # רשימה מורחבת של ביטויים שבוודאות לא קשורים לפודקאסטים
        definitely_not_podcast = [
            # מזג אוויר
            'מה מזג האוויר', 'מזג האוויר', 'weather', 'טמפרטורה', 
            'גשם', 'שמש', 'שלג', 'רוח', 'עננים', 'חם', 'קר',
            
            # זמן ותאריכים
            'מה השעה', 'איזה שעה', 'מה הזמן', 'מתי', 'תאריך',
            'יום', 'חודש', 'שנה', 'מחר', 'אתמול',
            
            # ברכות וחברותיות
            'היי', 'שלום', 'hello', 'hi', 'בוקר טוב', 'לילה טוב',
            'מה שלומך', 'איך הולך', 'מה נשמע',
            
            # תודות
            'תודה', 'thanks', 'תודה רבה', 'אין בעד מה',
            
            # שאלות זהות
            'איך קוראים לך', 'מי אתה', 'מה השם שלך', 'מה אתה',
            
            # ניווט ומיקום
            'איך מגיעים', 'איפה נמצא', 'דרך', 'נסיעה', 'כתובת',
            
            # חגים ואירועים
            'מתי פסח', 'מתי ראש השנה', 'חג', 'חגים',
            
            # שאלות כלליות
            'למה', 'איך', 'מתי', 'איפה', 'מי', 'מה זה',
            
            # בעיות טכניות
            'לא עובד', 'שגיאה', 'בעיה', 'תקלה'
        ]
        
        # בדיקה אם יש התאמה מדויקת
        for phrase in definitely_not_podcast:
            if phrase in text_lower:
                return {
                    "is_podcast_related": False,
                    "confidence": 0.95,
                    "reason": f"מכיל ביטוי שלא קשור לפודקאסטים: '{phrase}'"
                }
        
        # בדיקה אם זה רק שאלה קצרה (פחות מ-4 מילים) ללא מילות מפתח של פודקאסטים
        words = text_lower.split()
        if len(words) <= 3:
            podcast_keywords = [
                'פודקאסט', 'podcast', 'לשמוע', 'האזנה', 'תוכנית',
                'שיחות', 'ראיונות', 'תוכן', 'אודיו', 'רדיו', 'עניין'
            ]
            
            has_podcast_keyword = any(keyword in text_lower for keyword in podcast_keywords)
            if not has_podcast_keyword:
                return {
                    "is_podcast_related": False,
                    "confidence": 0.85,
                    "reason": "שאלה קצרה ללא מילות מפתח של פודקאסטים"
                }
        
        # אם לא מצאנו סיבה ברורה לדחות, נחזיר ביטחון נמוך
        return {
            "is_podcast_related": True,
            "confidence": 0.3,
            "reason": "לא נמצאה סיבה ברורה לדחיה"
        }
    
    def manual_relevance_check(self, user_text):
        """בדיקה ידנית כאשר GPT לא עובד"""
        text_lower = user_text.lower()
        
        # מילים שברור שלא קשורות לפודקאסטים
        non_podcast_keywords = [
            'מזג האוויר', 'weather', 'טמפרטורה', 'גשם', 'שמש',
            'מה השעה', 'זמן', 'תאריך', 'יום',
            'היי', 'שלום', 'hello', 'hi',
            'תודה', 'thanks', 'בוקר טוב', 'לילה טוב',
            'איך קוראים לך', 'מי אתה', 'מה זה',
            'איך מגיעים', 'נסיעה', 'דרך', 'מיקום'
        ]
        
        # אם יש מילת מפתח ברורה שלא קשורה לפודקאסטים
        if any(keyword in text_lower for keyword in non_podcast_keywords):
            return {
                "is_podcast_related": False,
                "confidence": 0.9,
                "reason": "מכיל מילות מפתח שלא קשורות לפודקאסטים"
            }
        
        # מילים שמרמזות על פודקאסטים
        podcast_keywords = [
            'פודקאסט', 'podcast', 'לשמוע', 'האזנה', 'תוכנית',
            'שיחות', 'ראיונות', 'תוכן אודיו', 'רדיו'
        ]
        
        # אם יש מילת מפתח ברורה לפודקאסטים
        if any(keyword in text_lower for keyword in podcast_keywords):
            return {
                "is_podcast_related": True,
                "confidence": 0.9,
                "reason": "מכיל מילות מפתח של פודקאסטים"
            }
        
        # במקרה של ספק - נניח שזה לא קשור לפודקאסטים
        return {
            "is_podcast_related": False,
            "confidence": 0.6,
            "reason": "לא מזוהה כבקשה לפודקאסט"
        }
    
    async def analyze_request(self, user_text, user_id):
        """ניתוח בקשת המשתמש עם GPT - כולל בדיקת רלוונטיות"""
        
        # קודם בודקים אם זה קשור לפודקאסטים
        relevance_check = await self.check_podcast_relevance(user_text)
        
        # אם זה לא קשור לפודקאסטים עם ביטחון גבוה
        if not relevance_check["is_podcast_related"] and relevance_check["confidence"] > 0.7:
            return {
                "is_podcast_related": False,
                "reason": relevance_check["reason"],
                "suggested_response": self.generate_non_podcast_response(user_text)
            }
        
        # אם זה קשור לפודקאסטים, ממשיכים לניתוח רגיל
        history = self.conversation_history.get(user_id, [])
        context = ""
        if history:
            context = f"היסטוריית שיחה קודמת: {history[-2:]}"
        
        system_prompt = f"""אתה עוזר המלצות פודקאסטים. נתח את הבקשה והחזר JSON עם המבנה הבא:

{context}

{{
    "topics": ["רשימת נושאים שמעניינים את המשתמש"],
    "duration_max": מספר דקות מקסימלי או null,
    "language_preference": "hebrew" או "english" או null,
    "keywords": ["מילות מפתח לחיפוש"],
    "user_intent": "תיאור כוונת המשתמש",
    "is_podcast_related": true
}}

חשוב:
- אם המשתמש מבקש "באנגלית" או "english" - language_preference: "english"
- אם המשתמש מבקש "בעברית" או לא מציין שפה - language_preference: "hebrew"
- אם המשתמש מציין זמן (10 דקות, 5 דקות) - duration_max: המספר"""

        try:
            response = await openai.ChatCompletion.acreate(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text}
                ],
                temperature=0.7,
                max_tokens=300
            )
            
            result = json.loads(response.choices[0].message.content)
            result["is_podcast_related"] = True
            
            # שמירת האינטראקציה
            if user_id not in self.conversation_history:
                self.conversation_history[user_id] = []
            self.conversation_history[user_id].append({
                'user_input': user_text,
                'analysis': result
            })
            
            # שמירה על היסטוריה מוגבלת
            if len(self.conversation_history[user_id]) > 5:
                self.conversation_history[user_id] = self.conversation_history[user_id][-5:]
            
            return result
            
        except Exception as e:
            print(f"GPT Analysis error: {e}")
            basic_result = self.basic_analysis(user_text)
            basic_result["is_podcast_related"] = True
            return basic_result
    
    def generate_non_podcast_response(self, user_text):
        """יוצר תגובה מתאימה לבקשות שלא קשורות לפודקאסטים"""
        
        text_lower = user_text.lower()
        
        if any(word in text_lower for word in ['מזג', 'weather', 'טמפרטורה']):
            return "🌤️ אני מתמחה רק במתן המלצות על פודקאסטים, לא במזג האוויר.\n\nאם אתה מחפש פודקאסט על מטאורולוגיה או מדעי האטמוספירה - אני אשמח לעזור! 🎧"
        
        elif any(word in text_lower for word in ['שעה', 'זמן', 'תאריך']):
            return "⏰ אני לא יודע מה השעה, אבל אני יכול להמליץ לך על פודקאסטים מעולים!\n\nמה מעניין אותך לשמוע? 🎧"
        
        elif any(word in text_lower for word in ['היי', 'שלום', 'hello']):
            return "👋 שלום! אני SHMALI - הבוט להמלצות פודקאסטים!\n\nספר לי איזה נושא מעניין אותך ואמצא לך פודקאסט מושלם! 🎧"
        
        elif any(word in text_lower for word in ['תודה', 'thanks']):
            return "😊 אין בעד מה! האם תרצה עוד המלצות על פודקאסטים? פשוט ספר לי איזה נושא מעניין אותך! 🎧"
        
        elif any(word in text_lower for word in ['דרך', 'נסיעה', 'מיקום']):
            return "🗺️ אני לא מומחה בניווט, אבל אני יכול להמליץ לך על פודקאסטים נהדרים לדרך!\n\nאיזה נושא תרצה לשמוע בנסיעה? 🎧"
        
        else:
            return f"🤖 אני מתמחה רק במתן המלצות על פודקאסטים.\n\nאם '{user_text}' קשור לנושא שתרצה לשמוע עליו בפודקאסט - ספר לי יותר פרטים! 🎧"
    
    def basic_analysis(self, text):
        """ניתוח בסיסי אם GPT נכשל"""
        text_lower = text.lower()
        
        # זיהוי שפה
        language_preference = None
        if any(word in text_lower for word in ['באנגלית', 'אנגלית', 'english', 'in english']):
            language_preference = 'english'
        elif any(word in text_lower for word in ['בעברית', 'עברית']):
            language_preference = 'hebrew'
        else:
            language_preference = 'hebrew'
        
        # זיהוי משך זמן
        duration_match = re.search(r'(\d+)\s*דק', text)
        duration_max = int(duration_match.group(1)) if duration_match else None
        
        # זיהוי נושאים
        topics = []
        topic_keywords = {
            'ספורט': ['ספורט', 'כדורגל', 'כדורסל', 'אימון'],
            'טכנולוגיה': ['טכנולוגיה', 'מחשב', 'תכנות', 'אפליקציה'],
            'בריאות': ['בריאות', 'תזונה', 'דיאטה', 'רפואה'],
            'קומדיה': ['קומדיה', 'מצחיק', 'הומור', 'צחוק'],
            'חדשות': ['חדשות', 'פוליטיקה', 'אקטואליה'],
            'עסקים': ['עסקים', 'כסף', 'יזמות', 'השקעות']
        }
        
        for topic, keywords in topic_keywords.items():
            if any(keyword in text_lower for keyword in keywords):
                topics.append(topic)
        
        return {
            "topics": topics,
            "duration_max": duration_max,
            "language_preference": language_preference,
            "keywords": text_lower.split()[:3],
            "user_intent": text[:100]
        }

class SimilarityScorer:
    """מחלקה לחישוב ציון דמיון 70%-30%"""
    
    @staticmethod
    def calculate_text_similarity(text1, text2):
        """חישוב דמיון טקסטואלי בין שני טקסטים"""
        return SequenceMatcher(None, text1.lower(), text2.lower()).ratio()
    
    @staticmethod
    def calculate_topic_similarity(user_analysis, podcast_data):
        """חישוב התאמה לנושאים עיקריים (70% מהציון)"""
        user_topics = user_analysis.get('topics', [])
        user_keywords = user_analysis.get('keywords', [])
        
        podcast_name = podcast_data.get('name', '').lower()
        podcast_description = podcast_data.get('description', '').lower()
        podcast_publisher = podcast_data.get('publisher', '').lower()
        
        # חיפוש התאמות ישירות
        direct_matches = 0
        total_terms = len(user_topics) + len(user_keywords)
        
        if total_terms == 0:
            return 0.5  # ציון נייטרלי אם אין נושאים
        
        # בדיקת נושאים
        for topic in user_topics:
            topic_lower = topic.lower()
            if (topic_lower in podcast_name or 
                topic_lower in podcast_description or 
                topic_lower in podcast_publisher):
                direct_matches += 2  # נושאים מקבלים ציון כפול
        
        # בדיקת מילות מפתח
        for keyword in user_keywords:
            keyword_lower = keyword.lower()
            if (keyword_lower in podcast_name or 
                keyword_lower in podcast_description or 
                keyword_lower in podcast_publisher):
                direct_matches += 1
        
        # חישוב ציון דמיון טקסטואלי
        name_similarity = SimilarityScorer.calculate_text_similarity(
            ' '.join(user_topics + user_keywords), 
            podcast_name
        )
        description_similarity = SimilarityScorer.calculate_text_similarity(
            ' '.join(user_topics + user_keywords), 
            podcast_description
        )
        
        # משקל לחישוב הסופי
        direct_score = min(direct_matches / (total_terms * 2), 1.0)  # נרמול ל-1
        similarity_score = max(name_similarity, description_similarity)
        
        # ציון סופי לנושאים
        topic_score = (direct_score * 0.7) + (similarity_score * 0.3)
        
        return min(topic_score, 1.0)
    
    @staticmethod
    def calculate_metadata_similarity(user_analysis, podcast_data):
        """חישוב התאמה למטאדטה (30% מהציון)"""
        metadata_score = 0.0
        checks = 0
        
        # בדיקת שפה (50% מהמטאדטה)
        user_language = user_analysis.get('language_preference')
        podcast_languages = podcast_data.get('languages', [])
        
        if user_language:
            checks += 1
            if user_language == 'hebrew' and ('he' in podcast_languages or 'iw' in podcast_languages):
                metadata_score += 0.5
            elif user_language == 'english' and any('en' in lang for lang in podcast_languages):
                metadata_score += 0.5
            # אם אין התאמה בשפה, לא מוסיפים ציון
        
        # בדיקת משך זמן (30% מהמטאדטה)
        user_max_duration = user_analysis.get('duration_max')
        podcast_duration = podcast_data.get('duration_minutes')
        
        if user_max_duration and podcast_duration:
            checks += 1
            if podcast_duration <= user_max_duration:
                # ציון גבוה יותר לפודקאסטים קצרים יותר (יותר טוב)
                duration_ratio = 1 - (podcast_duration / user_max_duration)
                metadata_score += 0.3 * (0.5 + 0.5 * duration_ratio)
            # אם הפודקאסט ארוך מדי, לא מוסיפים ציון
        
        # בדיקת כמות פרקים (20% מהמטאדטה)
        total_episodes = podcast_data.get('total_episodes', 0)
        if total_episodes > 0:
            checks += 1
            # העדפה לפודקאסטים עם כמות סבירה של פרקים
            if 5 <= total_episodes <= 100:
                metadata_score += 0.2
            elif total_episodes > 100:
                metadata_score += 0.1  # ציון נמוך יותר לפודקאסטים עם הרבה מאוד פרקים
        
        # אם לא היו בדיקות, מחזירים ציון נייטרלי
        return metadata_score if checks > 0 else 0.5
    
    @staticmethod
    def calculate_similarity_score(user_analysis, podcast_data):
        """חישוב ציון דמיון מסופי (70% נושאים + 30% מטאדטה)"""
        
        # 70% - התאמה לנושאים עיקריים
        topic_score = SimilarityScorer.calculate_topic_similarity(user_analysis, podcast_data)
        
        # 30% - התאמה למטאדטה
        metadata_score = SimilarityScorer.calculate_metadata_similarity(user_analysis, podcast_data)
        
        # ציון מסופי
        final_score = (topic_score * 0.7) + (metadata_score * 0.3)
        
        return round(final_score, 3)

class ShmaliBot:
    def __init__(self):
        self.spotify = SpotifyAPI(CLIENT_ID, CLIENT_SECRET)
        self.gpt_analyzer = GPTAnalyzer()
        self.similarity_scorer = SimilarityScorer()
        self.load_local_data()
        self.shown_recommendations = {}
        self.available_recommendations = {}
    
    def load_local_data(self):
        """טעינת נתונים מקומיים"""
        try:
            self.df = pd.read_csv(DATASET_CSV)
            print(f"✅ נטענו {len(self.df)} פודקאסטים מקומיים")
        except FileNotFoundError:
            print("⚠️ לא נמצא קובץ נתונים מקומי")
            self.df = pd.DataFrame()
    
    def search_local_dataset(self, analysis):
        """חיפוש בדטהסט המקומי עם דירוג similarity"""
        if self.df.empty:
            return []
        
        local_results = []
        topics = analysis.get('topics', [])
        keywords = analysis.get('keywords', [])
        language_pref = analysis.get('language_preference')
        max_duration = analysis.get('duration_max')
        
        for _, podcast in self.df.iterrows():
            # המרה לפורמט סטנדרטי
            podcast_data = {
                'name': podcast.get('name', 'Unknown'),
                'publisher': podcast.get('publisher', 'Unknown'),
                'description': str(podcast.get('description', '')),
                'url': podcast.get('url', '#'),
                'duration_minutes': podcast.get('duration_minutes'),
                'languages': [podcast.get('language', '')] if pd.notna(podcast.get('language')) else [],
                'total_episodes': podcast.get('total_episodes', 0),
                'source': 'local_dataset'
            }
            
            # חישוב ציון דמיון
            similarity_score = self.similarity_scorer.calculate_similarity_score(analysis, podcast_data)
            
            # סף מינימלי לקבלת פודקאסט
            if similarity_score >= 0.3:
                podcast_data['similarity_score'] = similarity_score
                local_results.append(podcast_data)
        
        # מיון לפי ציון דמיון
        local_results.sort(key=lambda x: x['similarity_score'], reverse=True)
        
        return local_results
    
    async def get_recommendations(self, analysis, user_id):
        """קבלת המלצות משילוב של Spotify והדטהסט המקומי עם דירוג similarity"""
        
        # Debug - מה יש לנו
        print(f"🔍 Getting recommendations for user {user_id}")
        print(f"   Topics: {analysis.get('topics', [])}")
        print(f"   Keywords: {analysis.get('keywords', [])}")
        
        # אם זו בקשה ראשונה או שהמשתמש ביקש נושא חדש, נאפס את הרשימות
        if user_id not in self.available_recommendations or self.is_new_topic(analysis, user_id):
            print(f"🔄 Resetting recommendations for user {user_id} (new topic or first time)")
            
            self.shown_recommendations[user_id] = []
            self.available_recommendations[user_id] = []
            
            all_recommendations = []
            
            # חיפוש בספוטיפיי קודם
            topics = analysis.get('topics', [])
            if topics:
                print(f"🎵 Searching Spotify for topics: {topics}")
                for topic in topics:
                    try:
                        spotify_results = self.spotify.search_podcasts(
                            query=topic,
                            max_duration=analysis.get('duration_max'),
                            language_preference=analysis.get('language_preference'),
                            limit=10
                        )
                        print(f"   Found {len(spotify_results)} results for topic '{topic}'")
                        for result in spotify_results:
                            result['source'] = 'spotify'
                            # חישוב ציון דמיון לתוצאות Spotify
                            result['similarity_score'] = self.similarity_scorer.calculate_similarity_score(analysis, result)
                        all_recommendations.extend(spotify_results)
                    except Exception as e:
                        print(f"❌ Error searching Spotify for topic '{topic}': {e}")
            
            # אם אין נושאים ספציפיים, חפש לפי מילות מפתח
            if len(all_recommendations) < 5 and analysis.get('keywords'):
                print(f"🔍 Searching by keywords: {analysis['keywords']}")
                query = ' '.join(analysis['keywords'][:2])
                try:
                    spotify_results = self.spotify.search_podcasts(
                        query=query,
                        max_duration=analysis.get('duration_max'),
                        language_preference=analysis.get('language_preference'),
                        limit=10
                    )
                    print(f"   Found {len(spotify_results)} results for keywords")
                    for result in spotify_results:
                        result['source'] = 'spotify'
                        result['similarity_score'] = self.similarity_scorer.calculate_similarity_score(analysis, result)
                    all_recommendations.extend(spotify_results)
                except Exception as e:
                    print(f"❌ Error searching Spotify by keywords: {e}")
            
            # חיפוש בדטהסט המקומי אחרי Spotify
            print(f"📁 Searching local dataset...")
            try:
                local_results = self.search_local_dataset(analysis)
                print(f"   Found {len(local_results)} local results")
                all_recommendations.extend(local_results)
            except Exception as e:
                print(f"❌ Error searching local dataset: {e}")
            
            # הסרת כפילויות
            seen = set()
            unique_recommendations = []
            for rec in all_recommendations:
                if rec['name'] not in seen:
                    seen.add(rec['name'])
                    unique_recommendations.append(rec)
            
            print(f"📊 Total unique recommendations: {len(unique_recommendations)}")
            
            # מיון לפי ציון דמיון במקום ערבוב רנדומלי
            unique_recommendations.sort(key=lambda x: x.get('similarity_score', 0), reverse=True)
            
            # שמירה עם לוג ציונים
            print(f"📊 Top recommendations for user {user_id}:")
            for i, rec in enumerate(unique_recommendations[:5]):
                print(f"  {i+1}. {rec['name']} - Score: {rec.get('similarity_score', 'N/A')}")
            
            self.available_recommendations[user_id] = unique_recommendations
        else:
            print(f"♻️ Using existing recommendations for user {user_id}")
        
        # מחזירים המלצה אחת שעוד לא הוצגה
        available_recs = self.available_recommendations.get(user_id, [])
        shown_recs = self.shown_recommendations.get(user_id, [])
        
        print(f"📝 Available: {len(available_recs)}, Shown: {len(shown_recs)}")
        
        for rec in available_recs:
            if rec['name'] not in shown_recs:
                self.shown_recommendations[user_id].append(rec['name'])
                print(f"✅ Returning recommendation: {rec['name']}")
                return [rec]
        
        # אם נגמרו ההמלצות
        print(f"❌ No more recommendations available for user {user_id}")
        return []
    
    def is_new_topic(self, analysis, user_id):
        """בודק אם המשתמש מבקש נושא חדש"""
        if user_id not in self.gpt_analyzer.conversation_history:
            return True
        
        # משווים את הנושאים הנוכחיים לקודמים
        history = self.gpt_analyzer.conversation_history.get(user_id, [])
        if len(history) < 2:
            return False
        
        current_topics = set(analysis.get('topics', []))
        previous_topics = set(history[-2]['analysis'].get('topics', []))
        
        return current_topics != previous_topics

def create_personalized_intro(analysis, is_more_request):
    """יצירת הקדמה אישית על בסיס הניתוח"""
    topics = analysis.get('topics', [])
    duration = analysis.get('duration_max')
    language = analysis.get('language_preference')
    
    if is_more_request:
        intro = "הנה עוד המלצה"
    else:
        intro = "הנה מה שמצאתי בשבילך"
    
    # הוספת פרטים ספציפיים
    if topics and not is_more_request:
        intro += f" - פודקאסט על {', '.join(topics)}"
    
    if duration and not is_more_request:
        intro += f" עד {duration} דקות"
    
    if language == 'english' and not is_more_request:
        intro += " באנגלית"
    elif language == 'hebrew' and not is_more_request:
        intro += " בעברית"
    
    intro += ":"
    return intro

def format_single_recommendation(recommendation, intro_message):
    """עיצוב המלצה בודדת לתצוגה עם הקדמה אישית וציון דמיון"""
    if not recommendation:
        return "😔 נגמרו ההמלצות בנושא זה. נסה לחפש נושא אחר או תאר אחרת את מה שאתה מחפש."
    
    rec = recommendation[0]
    
    # השתמש בהקדמה האישית
    text = intro_message + "\n" + "=" * 30 + "\n"
    
    text += f"\n🎧 **{rec['name']}**\n"
    text += f"👤 מאת: {rec['publisher']}\n"
     
    # שפה
    languages = rec.get('languages', [])
    if 'he' in languages or 'iw' in languages:
        text += f"🗣️ שפה: עברית\n"
    elif 'en' in languages:
        text += f"🗣️ שפה: אנגלית\n"
    
    # משך זמן
    if rec.get('duration_minutes'):
        text += f"⏱️ משך: {rec['duration_minutes']} דקות\n"
    
    # תיאור
    description = rec['description'][:200] + "..." if len(rec['description']) > 200 else rec['description']
    text += f"📝 {description}\n"
    text += f"🔗 [להאזנה]({rec['url']})\n"
    
    text += "\n" + "=" * 30 + "\n"
    text += "💡 רוצה עוד המלצה? פשוט תכתוב 'עוד' או 'עוד המלצה'\n"
    text += "🔍 לחיפוש נושא חדש - פשוט תכתוב מה מעניין אותך\n"
    text += "🔄 להתחלה מחדש: /reset"
    
    return text

# Telegram Bot Functions
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_message = """🎧 היי! אני SHMALI - הבוט שלך להמלצות פודקאסטים!

💬 ספר לי במדויק מה אתה מחפש:

דוגמאות:
• "רוצה לשמוע פודקאסט על ספורט"
• "רוצה לשמוע פודקאסט על בורסה בעברית עד חצי שעה"
• "פודקאסט על טכנולוגיה באנגלית"
• "פודקאסט בעברית על פסיכולוגיה עד שעה"

איך אני יכול לעזור לך?
*אני לא עובד אם לא תרשום לי את התחום שבו אתה מחפש*"""
   
    await update.message.reply_text(welcome_message)

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """איפוס השיחה"""
    user_id = update.effective_user.id
    bot_instance = context.bot_data['bot_instance']
    if user_id in bot_instance.gpt_analyzer.conversation_history:
        del bot_instance.gpt_analyzer.conversation_history[user_id]
    if user_id in bot_instance.shown_recommendations:
        del bot_instance.shown_recommendations[user_id]
    if user_id in bot_instance.available_recommendations:
        del bot_instance.available_recommendations[user_id]

    welcome_message = """🎧 היי! אני SHMALI - הבוט שלך להמלצות פודקאסטים!

💬 ספר לי במדויק מה אתה מחפש:

דוגמאות:
• "רוצה לשמוע פודקאסט על ספורט"
• "רוצה לשמוע פודקאסט על בורסה בעברית עד חצי שעה"
• "פודקאסט על טכנולוגיה באנגלית"
• "פודקאסט בעברית על פסיכולוגיה עד שעה"

איך אני יכול לעזור לך?
*אני לא עובד אם לא תרשום לי את התחום שבו אתה מחפש*"""

    await update.message.reply_text("🔄 השיחה אופסה! \n\n" + welcome_message)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """טיפול בהודעות טקסט עם בדיקת רלוונטיות לפודקאסטים"""
    user_text = update.message.text
    user_id = update.effective_user.id
    bot_instance = context.bot_data['bot_instance']
    
    # בדיקה אם המשתמש מבקש עוד המלצות
    is_more_request = any(word in user_text.lower() for word in ['עוד', 'המלצה נוספת', 'עוד המלצה', 'הבא', 'next'])
    
    # אם מבקשים עוד והיה ניתוח קודם
    if is_more_request and user_id in bot_instance.gpt_analyzer.conversation_history:
        history = bot_instance.gpt_analyzer.conversation_history[user_id]
        if history:
            # השתמש בניתוח האחרון
            analysis = history[-1]['analysis']
            
            # וודא שהניתוח הקודם היה על פודקאסטים
            if not analysis.get('is_podcast_related', True):
                await update.message.reply_text("🤔 לא מצאתי היסטוריה של חיפוש פודקאסטים. אנא תאר איזה פודקאסט אתה מחפש.")
                return
                
            # המשך עם הניתוח הקיים
            print(f"📱 User {user_id} requested 'more' - using existing analysis")
        else:
            await update.message.reply_text("🤔 לא מצאתי היסטוריה של חיפוש. אנא תאר מה אתה מחפש.")
            return
    else:
        # רק אם זו לא בקשה ל"עוד" - עשה בדיקת שפה וניתוח חדש
        
        # בדיקה שההודעה בעברית
        hebrew_chars = sum(1 for char in user_text if '\u0590' <= char <= '\u05FF')
        if hebrew_chars < len(user_text) * 0.2:
            await update.message.reply_text(
                "🤖 אני מבין רק עברית. אנא כתוב בעברית מה אתה מחפש."
            )
            return
        
        # ניתוח הבקשה החדשה
        print(f"📱 User {user_id} sent new request: '{user_text}' - analyzing...")
        analysis = await bot_instance.gpt_analyzer.analyze_request(user_text, user_id)
        
        # בדיקה אם הבקשה קשורה לפודקאסטים
        if not analysis.get('is_podcast_related', True):
            suggested_response = analysis.get('suggested_response', 
                "🤖 אני מתמחה רק במתן המלצות על פודקאסטים.\n\nספר לי איזה נושא מעניין אותך לשמוע בפודקאסט! 🎧")
            await update.message.reply_text(suggested_response)
            return
    
    await update.message.chat.send_action("typing")
    
    try:
        # קבלת המלצה אחת
        recommendation = await bot_instance.get_recommendations(analysis, user_id)
        
        # יצירת הקדמה אישית על בסיס הניתוח
        intro_message = create_personalized_intro(analysis, is_more_request)
        
        # בניית התגובה המאוחדת
        response = format_single_recommendation(recommendation, intro_message)
        
        await update.message.reply_text(
            response,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        
        print(f"✅ Sent recommendation to user {user_id}")
        
    except Exception as e:
        print(f"❌ Error handling message: {e}")
        await update.message.reply_text(
            "😅 משהו השתבש. תנסה שוב?"
        )

def main():
    # בדיקת הגדרות
    if OPENAI_API_KEY == 'YOUR_OPENAI_API_KEY_HERE':
        print("❌ יש להגדיר את OPENAI_API_KEY!")
        return
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    bot = ShmaliBot()
    app.bot_data["bot_instance"] = bot
    
    # הוספת handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🚀 SHMALI Bot מתחיל!")
    print("🤖 GPT מופעל עם בדיקת רלוונטיות")
    print("🔤 זיהוי שפה משופר")
    print("📊 מערכת Similarity Scoring (70%-30%)")
    print("🎯 דירוג חכם של תוצאות")
    print("🔄 הגנה מפני בקשות לא רלוונטיות")
    print("🐛 מצב Debug מופעל - תראה לוגים מפורטים")
    
    app.run_polling()

if __name__ == '__main__':
    main()
