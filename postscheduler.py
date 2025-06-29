import os
import threading
import time
import atexit
import dotenv
from flask import Flask, request, jsonify, render_template
from werkzeug.utils import secure_filename
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
import praw
from praw.exceptions import APIException
import cohere

dotenv.load_dotenv()

# ---------- Reddit and Cohere Configuration ----------
reddit = praw.Reddit(
    client_id=os.getenv('REDDIT_CLIENT_ID'),
    client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
    username=os.getenv('REDDIT_USERNAME'),
    password=os.getenv('REDDIT_PASSWORD'),
    user_agent=os.getenv('REDDIT_USER_AGENT')
)

cohere_client = cohere.Client(os.getenv("COHERE_API_KEY"))

# ---------- Logging ----------
LOG_FILE = open("postscheduler.log", "a+")

def log(msg):
    print(msg)
    LOG_FILE.write(msg + '\n')
    LOG_FILE.flush()

def tolink(permalink):
    return "https://reddit.com" + permalink

# ---------- Flask App Setup ----------
UPLOAD_FOLDER = 'uploads'
DB_URL = 'sqlite:///jobs.sqlite'

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
atexit.register(lambda: LOG_FILE.close())

# ---------- APScheduler Setup ----------
scheduler = BackgroundScheduler({
    'jobstores': {
        'default': SQLAlchemyJobStore(url=DB_URL)
    }
})
scheduler.start()

# ---------- Job Handler ----------
def submitPost(**kwargs):
    try:
        subreddit = reddit.subreddit(kwargs['sub'])
        submission = None

        if kwargs['video']:
            submission = subreddit.submit_video(
                title=kwargs['title'],
                video_path=kwargs['video'],
                thumbnail_path=kwargs['image'],
                send_replies=True
            )
        elif kwargs['image']:
            submission = subreddit.submit_image(
                title=kwargs['title'],
                image_path=kwargs['image'],
                send_replies=True
            )
        elif kwargs['link']:
            submission = subreddit.submit(
                title=kwargs['title'],
                url=kwargs['link'],
                send_replies=True
            )
        else:
            submission = subreddit.submit(
                title=kwargs['title'],
                selftext=kwargs.get('text', ''),
                send_replies=True
            )

        log(f"[SUCCESS] Posted to r/{kwargs['sub']}. Link: {tolink(submission.permalink)}")
        start_comment_monitoring(
            kwargs['sub'],
            submission.id,
            like_comments=kwargs.get('like_comments', False),
            reply_to_comments=kwargs.get('reply_to_comments', False),
            reply_message=kwargs.get('reply_message', '')
        )

        return 0, tolink(submission.permalink)

    except APIException as e:
        log(f"[API ERROR] {str(e)}")
        return 1, None
    except Exception as e:
        log(f"[ERROR] {str(e)}")
        return 1, None

def scheduled_submit(post_kwargs):
    log(f"[RUNNING] Submitting post to r/{post_kwargs['sub']} with title: {post_kwargs['title']}")
    err, link = submitPost(**post_kwargs)
    if err == 0:
        log(f"[SUCCESS] Posted to r/{post_kwargs['sub']}. Link: {link}")
    else:
        log(f"[FAILED] Post to r/{post_kwargs['sub']} failed.")

@app.route('/submit', methods=['POST'])
def submit():
    try:
        form = request.form
        files = request.files
        scheduled_ms = form.get("scheduled_time")
        now = time.time()

        image_file = files.get('image')
        video_file = files.get('video')

        image_path = None
        if image_file and image_file.filename:
            filename = secure_filename(image_file.filename)
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)

        video_path = None
        if video_file and video_file.filename:
            filename = secure_filename(video_file.filename)
            video_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            video_file.save(video_path)

        run_timestamp = int(scheduled_ms) / 1000 if scheduled_ms else now
        job_id = f"{form.get('sub')}_{time.time()}"

        post_kwargs = {
            'sub': form.get("sub"),
            'title': form.get("title"),
            'text': form.get("text") or "",
            'link': form.get("link"),
            'image': image_path,
            'video': video_path,
            'like_comments': form.get("like_comments") == "on",
            'reply_to_comments': form.get("reply_to_comments") == "on",
            'reply_message': form.get("reply_message", "").strip()
        }

        # Post immediately if within 10 seconds
        if run_timestamp <= now + 10:
            log(f"[IMMEDIATE] Posting to r/{form.get('sub')} now.")
            err, link = submitPost(**post_kwargs)

            if err == 0:
                return jsonify({
                    "status": "success",
                    "job_id": None,
                    "scheduled_time": "Posted Immediately",
                    "link": link
                })
            else:
                return jsonify({"status": "error", "message": "Immediate post failed"}), 500

        # Otherwise, schedule
        run_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(run_timestamp))

        scheduler.add_job(
            scheduled_submit,
            'date',
            run_date=run_time,
            args=[post_kwargs],
            id=job_id,
            replace_existing=True
        )

        log(f"Scheduled job {job_id} for subreddit {form.get('sub')} at {run_time}")
        return jsonify({
            "status": "scheduled",
            "job_id": job_id,
            "scheduled_time": run_time
        })

    except Exception as e:
        log(f"[ERROR] Submission handler error -- {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

from praw.models.util import stream_generator
def start_comment_monitoring(subreddit_name, submission_id, like_comments=False, reply_to_comments=False, reply_message=""):
    def monitor():
        try:
            subreddit = reddit.subreddit(subreddit_name)
            log(f"[MONITORING] Watching r/{subreddit_name} for comments on post {submission_id}...")
            for comment in subreddit.stream.comments(skip_existing=True):
                if comment.author and comment.author.name != reddit.user.me().name:
                    log(f"[NEW COMMENT] u/{comment.author}: {comment.body}")

                    if like_comments:
                        comment.upvote()
                        log(f"[UPVOTED] Comment by u/{comment.author}")

                    if reply_to_comments:
                        try:
                            response = cohere_client.generate(
                                model='command-r-plus',
                                prompt=f"You are a user replying to your comments. Write a short, friendly, and relevant reply to the following comment:\n\n\"{comment.body}\"\n\nYour reply:",
                                max_tokens=60,
                                temperature=0.7
                            )
                            ai_reply = response.generations[0].text.strip()
                            if ai_reply:
                                comment.reply(ai_reply)
                                log(f"[AI REPLIED] to u/{comment.author}: {ai_reply}")
                        except Exception as e:
                            log(f"[ERROR] Cohere reply generation failed: {e}")

        except Exception as e:
            log(f"[ERROR in comment monitor]: {e}")

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()

# Store monitoring preferences globally (or in DB if needed)
friend_monitor_config = {
    'auto_like': True,
    'auto_summary': True,
    'auto_comment': True
}

@app.route('/monitor-settings', methods=['POST'])
def monitor_settings():
    try:
        form = request.form
        friend_monitor_config['auto_like'] = form.get("auto_like_friends") == "on"
        friend_monitor_config['auto_summary'] = form.get("auto_summary_friends") == "on"
        friend_monitor_config['auto_comment'] = form.get("auto_comment_friends") == "on"

        log(f"[SETTINGS] Updated friend monitoring: {friend_monitor_config}")

        return jsonify({"status": "success"})
    except Exception as e:
        log(f"[ERROR] Failed to update monitor settings: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

def load_friend_username():
    try:
        with open("friends.txt", "r") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        log("File Not Found - friends.txt")
        return []
@app.route('/')
def index():
    friends = load_friend_username()
    return render_template('form.html', friends=friends)

def monitor_friend_posts(auto_like_friends=True, auto_comment_friends=True, auto_summary_friends=True):
    def monitor():
        friends = load_friend_username()
        for username in friends:
            try:
                redditor = reddit.redditor(username)
                log(f"[Monitoring] Posts by u/{username}")
                for submission in redditor.stream.submissions(skip_existing=True):
                    log(f"[NEW POST] u/{username}: {submission.title}")
                    if auto_like_friends:
                        submission.upvote()
                        log(f"[LIKED] u/{username}'s post")
                    if auto_summary_friends:
                        prompt = f"Summarize this post in 2 lines:\n\nTitle: {submission.title}\nText: {submission.selftext}"
                        try:

                            response = cohere_client.generate(
                                model="command-r-plus",
                                prompt=prompt,
                                max_tokens=80,
                                temperature=0.7
                            )
                            summary = response.generations[0].text.strip()
                            log(f"[SUMMARY] {summary}")
                        except Exception as e:
                            log(f"[ERROR] Summary generation failed: {e}")
                    if auto_comment_friends:
                        prompt = (
                            "You are Sneh replying to a friend's Reddit post in a friendly tone. "
                            "Restrict yourself to only some short words that users might post as comments. "
                            "These words represent a variety of emotions—from happy, sad, surprised, supportive to sarcastic—as well as Gen-Z expressions. "
                            "They are suitable for all kinds of posts—photos, nature, bodybuilding, food, cars, formal announcements, or deep thoughtful text posts. "
                            "For formal announcements or job-related posts (e.g., promotions, new jobs), keep your reply polite, respectful, formal, emoji-free, and if the post uses plural subjects, your reply should too. "
                            "If the post is singular, use singular phrasing. "
                            "For informal or casual posts, feel free to use slang and emojis.\n\n"
                            "Allowed words also think it before putting it, other words are also allowed:\n"
                            "['congrats','wonderful','awesome','cheerful','great insights','wow', 'nice', 'cool', 'love', 'ily', '<3', 'yay', 'yaay', 'lit', '😄', '😊', '🔥', "
                            "'omg', 'woo', 'yass', 'fr', 'sheeesh', '🎉', 'congrats', 'boss', 'icon',"
                            "'facts', 'real', 'damn', 'cute', 'valid', 'W', "
                            "'sad', 'sigh', 'bruh', '😡',"
                            "'lame', 'ugh', 'nah', 'wtf', 'frfr', " 
                            "'oops', 'whoa', 'woah', '😳', '😬', 'lol', 'lmao', 'dead', '💀', "
                            "'hmm', '🤔', 'idk', 'deep', 'fr?', 'why', 'how', 'what?', 'rly?', "
                            "'respect', 'salute', '🫡', "
                            "''no cap', "
                            "'lmfao', 'same','Really broked up', 'fr', 'bet', 'based']\n\n"
                            f"Keep your reply very short and relevant.\n\n"
                            f"Post Title: {submission.title}\n"
                            f"Post Text: {submission.selftext}\n"
                            "Reply:"
                        )

                        try:
                            
                            response = cohere_client.generate(
                                model="command-r-plus",
                                prompt=prompt,
                                max_tokens=80,
                                temperature=0.7
                            )
                            comment_text = response.generations[0].text.strip()
                            submission.reply(comment_text)
                            log(f"[COMMENTED] on u/{username}'s post: {comment_text}")
                        except Exception as e:
                            log(f"[ERROR] Comment generation failed: {e}")
            except Exception as e:
                log(f"[ERROR] Skipping u/{username} due to: {e}")
                continue  # Skip to the next friend
    threading.Thread(target=monitor, daemon=True).start()



if __name__ == "__main__":
    monitor_friend_posts()
    log("Web mode running...")
    app.run(debug=True, use_reloader=False)
