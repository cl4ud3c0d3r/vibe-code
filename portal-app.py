from flask import Flask, render_template, request, jsonify, send_file, send_from_directory
import os
import zipfile
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import json
import base64
import uuid
from datetime import datetime

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024 * 1024  # 10GB limit

BASE_DIR = '/home/server'
UPLOAD_DIR = '/home/server/portal/dump'

upload_sessions = {}

@app.template_filter('timestamp_to_date')
def timestamp_to_date(timestamp):
    return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M')

def get_file_tree(path):
    items = []
    try:
        for item in sorted(os.listdir(path)):
            if item.startswith('.'):
                continue
            item_path = os.path.join(path, item)
            is_dir = os.path.isdir(item_path)
            try:
                size = os.path.getsize(item_path) if not is_dir else 0
                modified = os.path.getmtime(item_path)
                items.append({
                    'name': item,
                    'is_dir': is_dir,
                    'size': size,
                    'modified': modified,
                    'path': os.path.relpath(item_path, BASE_DIR)
                })
            except (OSError, IOError):
                continue
    except (OSError, IOError):
        pass
    return items

@app.route('/')
def index():
    current_path = request.args.get('path', '')
    full_path = os.path.join(BASE_DIR, current_path)
    
    if not full_path.startswith(BASE_DIR):
        full_path = BASE_DIR
        current_path = ''
    
    if not os.path.exists(full_path) or not os.path.isdir(full_path):
        full_path = BASE_DIR
        current_path = ''
    
    items = get_file_tree(full_path)
    
    breadcrumbs = []
    if current_path:
        parts = current_path.split('/')
        for i, part in enumerate(parts):
            breadcrumbs.append({
                'name': part,
                'path': '/'.join(parts[:i+1])
            })
    
    return render_template('index.html', 
                         items=items, 
                         current_path=current_path,
                         breadcrumbs=breadcrumbs)

@app.route('/download/<path:filepath>')
def download_file(filepath):
    full_path = os.path.join(BASE_DIR, filepath)
    if not full_path.startswith(BASE_DIR) or not os.path.exists(full_path):
        return "File not found", 404
    
    if os.path.isfile(full_path):
        return send_file(full_path, as_attachment=True)
    else:
        return "Cannot download directory directly", 400

@app.route('/download_zip/<path:dirpath>')
def download_zip(dirpath):
    full_path = os.path.join(BASE_DIR, dirpath)
    if not full_path.startswith(BASE_DIR) or not os.path.exists(full_path) or not os.path.isdir(full_path):
        return "Directory not found", 404
    
    temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
    
    def create_zip_chunk(file_list, chunk_num):
        chunk_zip = tempfile.NamedTemporaryFile(delete=False, suffix=f'_chunk{chunk_num}.zip')
        with zipfile.ZipFile(chunk_zip.name, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file_path, arc_path in file_list:
                if os.path.exists(file_path):
                    zf.write(file_path, arc_path)
        return chunk_zip.name
    
    all_files = []
    for root, dirs, files in os.walk(full_path):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for file in files:
            if file.startswith('.'):
                continue
            file_path = os.path.join(root, file)
            arc_path = os.path.relpath(file_path, full_path)
            all_files.append((file_path, arc_path))
    
    if len(all_files) > 20:
        chunks = [all_files[i::4] for i in range(4)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            chunk_files = list(executor.map(lambda x: create_zip_chunk(x[1], x[0]), enumerate(chunks)))
        
        with zipfile.ZipFile(temp_zip.name, 'w', zipfile.ZIP_DEFLATED) as main_zip:
            for chunk_file in chunk_files:
                with zipfile.ZipFile(chunk_file, 'r') as chunk_zip:
                    for info in chunk_zip.infolist():
                        main_zip.writestr(info, chunk_zip.read(info.filename))
                os.unlink(chunk_file)
    else:
        with zipfile.ZipFile(temp_zip.name, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file_path, arc_path in all_files:
                zf.write(file_path, arc_path)
    
    temp_zip.close()
    
    def cleanup_file(filepath):
        time.sleep(60)
        try:
            os.unlink(filepath)
        except:
            pass
    
    threading.Thread(target=cleanup_file, args=(temp_zip.name,)).start()
    
    return send_file(temp_zip.name, as_attachment=True, 
                    download_name=f"{os.path.basename(dirpath)}.zip")

@app.route('/uploads')
def uploads():
    return render_template('uploads.html')

@app.route('/start_upload', methods=['POST'])
def start_upload():
    data = request.get_json()
    filename = data.get('filename')
    filesize = data.get('filesize')
    
    session_id = str(uuid.uuid4())
    upload_sessions[session_id] = {
        'filename': filename,
        'filesize': filesize,
        'chunks_received': 0,
        'total_chunks': 4,
        'temp_files': []
    }
    
    return jsonify({'session_id': session_id})

@app.route('/upload_chunk', methods=['POST'])
def upload_chunk():
    session_id = request.form.get('session_id')
    chunk_num = int(request.form.get('chunk_num'))
    chunk_data = request.files.get('chunk')
    
    if session_id not in upload_sessions:
        return jsonify({'error': 'Invalid session'}), 400
    
    session = upload_sessions[session_id]
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f'_chunk{chunk_num}')
    chunk_data.save(temp_file.name)
    temp_file.close()
    
    session['temp_files'].append((chunk_num, temp_file.name))
    session['chunks_received'] += 1
    
    if session['chunks_received'] == session['total_chunks']:
        final_path = os.path.join(UPLOAD_DIR, session['filename'])
        session['temp_files'].sort(key=lambda x: x[0])
        
        with open(final_path, 'wb') as final_file:
            for _, temp_path in session['temp_files']:
                with open(temp_path, 'rb') as temp_file:
                    final_file.write(temp_file.read())
                os.unlink(temp_path)
        
        del upload_sessions[session_id]
        return jsonify({'status': 'complete'})
    
    return jsonify({'status': 'chunk_received'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=6565, debug=False)