import os
import subprocess
import uuid
import shutil
import time
import json
import threading
from flask import Flask, request, jsonify, render_template, send_from_directory, Response
from werkzeug.utils import secure_filename

app = Flask(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
OUTPUT_FOLDER = os.path.join(BASE_DIR, 'output')
ALLOWED_EXTENSIONS = {'ply'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER
app.config['PERMANENT_SESSION_LIFETIME'] = 86400 # 24 hours in seconds for potential session use
app.config['CLEANUP_INTERVAL_HOURS'] = 1 # How often to run cleanup check
app.config['CLEANUP_AGE_HOURS'] = 24 # How old completed tasks should be before deletion

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

conversion_tasks = {}
tasks_lock = threading.Lock()

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def perform_file_deletion(task_id, task_details):
    """
    Helper function to delete files associated with a task.
    Can be called by manual delete route or automatic cleanup.
    """
    if not task_details:
        return

    print(f"Performing file deletion for task: {task_id}")

    # Delete output .sogs file
    output_sogs_path = task_details.get("output_sogs_path")
    if output_sogs_path and os.path.exists(output_sogs_path):
        try:
            os.remove(output_sogs_path)
            print(f"Deleted output file: {output_sogs_path}")
        except OSError as e:
            print(f"Error deleting output file {output_sogs_path}: {e}")

    # Original .ply (should be None if conversion was successful and it was deleted)
    input_ply_path = task_details.get("input_ply_path")
    if input_ply_path and os.path.exists(input_ply_path):
        try:
            os.remove(input_ply_path)
            print(f"Deleted input file: {input_ply_path}")
        except OSError as e:
            print(f"Error deleting input file {input_ply_path}: {e}")

    # Cleanup empty directories
    def cleanup_empty_dirs(path_to_check, base_folder_path):
        if not path_to_check or not os.path.exists(path_to_check):
            return
        # Ensure we don't try to remove the base UPLOAD_FOLDER or OUTPUT_FOLDER itself
        if os.path.commonpath([path_to_check, base_folder_path]) != base_folder_path or path_to_check == base_folder_path:
            return

        try:
            if not os.listdir(path_to_check):
                os.rmdir(path_to_check)
                print(f"Removed empty directory: {path_to_check}")
                # Recursively try to clean up parent directory
                cleanup_empty_dirs(os.path.dirname(path_to_check), base_folder_path)
        except OSError as e:
            # This can happen if another process/thread accesses dir, or if it's not actually empty due to hidden files
            print(f"Error removing or checking directory {path_to_check}: {e}")

    client_identifier = task_details.get("client_identifier", "")
    if client_identifier and os.path.dirname(client_identifier): # If there was a path component
        # Secure the path components before joining
        relative_dir_parts = [secure_filename(part) for part in os.path.dirname(client_identifier).split(os.sep) if part and part != '.']
        if relative_dir_parts:
            upload_sub_dir_leaf = os.path.join(UPLOAD_FOLDER, *relative_dir_parts)
            output_sub_dir_leaf = os.path.join(OUTPUT_FOLDER, *relative_dir_parts)
            cleanup_empty_dirs(upload_sub_dir_leaf, UPLOAD_FOLDER)
            cleanup_empty_dirs(output_sub_dir_leaf, OUTPUT_FOLDER)


def run_sogs_conversion(task_id, input_ply_path, output_sogs_path, original_filename):
    global conversion_tasks

    output_dir_for_sogs = os.path.dirname(output_sogs_path)
    cmd = ['sogs-compress', '--ply', input_ply_path, '--output-dir', output_dir_for_sogs]

    try:
        with tasks_lock:
            conversion_tasks[task_id]["status"] = "converting"
            conversion_tasks[task_id]["message"] = "Conversion starting..."
            conversion_tasks[task_id]["progress"] = 0
            conversion_tasks[task_id]["updated_at"] = time.time()


        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True)
        for line in process.stdout:
            line_strip = line.strip()
            if not line_strip: continue

            print(f"SOGS output for {task_id}: {line_strip}")
            progress_val = None
            if '%' in line_strip:
                parts = line_strip.split('%')
                if len(parts) > 0:
                    potential_progress = parts[0].split()[-1]
                    potential_progress = ''.join(filter(str.isdigit, potential_progress))
                    if potential_progress.isdigit():
                        progress_val = int(potential_progress)

            with tasks_lock:
                conversion_tasks[task_id]["message"] = line_strip
                if progress_val is not None and progress_val <= 100 :
                    conversion_tasks[task_id]["progress"] = progress_val
                conversion_tasks[task_id]["updated_at"] = time.time()

        process.wait()

        with tasks_lock:
            task_data = conversion_tasks[task_id]
            task_data["updated_at"] = time.time()
            if process.returncode == 0:
                task_data["status"] = "completed"
                task_data["progress"] = 100
                task_data["message"] = "Conversion successful."
                task_data["completed_at"] = time.time() # Timestamp for cleanup
                try:
                    os.remove(input_ply_path)
                    task_data["input_ply_path"] = None
                except OSError as e:
                    print(f"Error deleting original file {input_ply_path}: {e}")
                    task_data["message"] += f" (Warning: Failed to delete {original_filename})"
            else:
                task_data["status"] = "failed"
                error_message = task_data["message"]
                if not error_message or "Error" not in error_message:
                     error_message = f"Conversion failed. Process exited with code: {process.returncode}"
                task_data["message"] = error_message

    except FileNotFoundError:
        with tasks_lock:
            if task_id in conversion_tasks:
                conversion_tasks[task_id]["status"] = "failed"
                conversion_tasks[task_id]["message"] = "Error: sogs-compress command not found."
                conversion_tasks[task_id]["updated_at"] = time.time()
    except Exception as e:
        with tasks_lock:
            if task_id in conversion_tasks:
                conversion_tasks[task_id]["status"] = "failed"
                conversion_tasks[task_id]["message"] = f"An unexpected error during conversion: {str(e)}"
                conversion_tasks[task_id]["updated_at"] = time.time()
    finally:
        print(f"Conversion thread for {task_id} finished.")


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file_route():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['file']
    # Use provided relative_path, default to filename if not provided (e.g. single file drop)
    relative_path = request.form.get('relative_path', file.filename)

    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file and allowed_file(file.filename):
        original_filename = secure_filename(file.filename)
        task_id = str(uuid.uuid4())

        current_time = time.time()
        path_dirname = os.path.dirname(relative_path)
        relative_path_parts = [secure_filename(part) for part in path_dirname.split(os.sep) if part and part != '.'] if path_dirname else []

        upload_sub_dir = os.path.join(app.config['UPLOAD_FOLDER'], *relative_path_parts)
        output_sub_dir = os.path.join(app.config['OUTPUT_FOLDER'], *relative_path_parts)

        os.makedirs(upload_sub_dir, exist_ok=True)
        os.makedirs(output_sub_dir, exist_ok=True)

        input_ply_path = os.path.join(upload_sub_dir, original_filename)
        output_sogs_filename = original_filename.rsplit('.', 1)[0] + '.sogs'
        output_sogs_path = os.path.join(output_sub_dir, output_sogs_filename)

        file.save(input_ply_path)

        with tasks_lock:
            conversion_tasks[task_id] = {
                "task_id": task_id,
                "status": "uploaded",
                "original_filename": original_filename,
                "client_identifier": relative_path,
                "input_ply_path": input_ply_path,
                "output_sogs_path": output_sogs_path,
                "progress": 0,
                "message": "File uploaded, queued for conversion.",
                "created_at": current_time,
                "updated_at": current_time
            }

        conversion_thread = threading.Thread(target=run_sogs_conversion, args=(task_id, input_ply_path, output_sogs_path, original_filename))
        conversion_thread.start()

        return jsonify({
            "message": "File upload successful, conversion started.",
            "task_id": task_id,
            "client_identifier": relative_path,
            "initial_status": conversion_tasks[task_id]
        }), 202
    else:
        return jsonify({"error": "File type not allowed"}), 400

@app.route('/stream/<task_id>')
def stream_status(task_id):
    def generate():
        last_sent_state_json = "" # Store the whole JSON state to detect any change

        while True:
            current_task_state = {}
            with tasks_lock:
                task = conversion_tasks.get(task_id)
                if task: # Make a copy to work with outside the lock if needed for complex logic
                    current_task_state = task.copy()

            if not current_task_state:
                yield f"data: {json.dumps({'error': 'Task not found or removed', 'status': 'error', 'task_id': task_id})}\n\n"
                break

            # Prepare data to send, ensuring all relevant fields are included
            data_to_send = {
                "task_id": task_id,
                "status": current_task_state.get("status", "unknown"),
                "progress": current_task_state.get("progress", 0),
                "message": current_task_state.get("message", ""),
                "original_filename": current_task_state.get("original_filename"),
                "client_identifier": current_task_state.get("client_identifier")
            }
            if data_to_send["status"] == "completed":
                data_to_send["download_url"] = f"/download/{task_id}"

            current_state_json = json.dumps(data_to_send)

            if current_state_json != last_sent_state_json:
                yield f"data: {current_state_json}\n\n"
                last_sent_state_json = current_state_json

            if data_to_send["status"] in ["completed", "failed", "error"]:
                break

            time.sleep(0.5)
    return Response(generate(), mimetype='text/event-stream')


@app.route('/status/<task_id>')
def get_status(task_id):
    with tasks_lock:
        task = conversion_tasks.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task)

@app.route('/download/<task_id>')
def download_sogs_file(task_id):
    with tasks_lock:
        task = conversion_tasks.get(task_id)

    if not task or task.get("status") != "completed":
        return jsonify({"error": "File not ready or task not found"}), 404

    output_sogs_path = task.get("output_sogs_path")
    if not output_sogs_path or not os.path.exists(output_sogs_path):
         return jsonify({"error": "Converted file not found on server."}), 404

    directory = os.path.dirname(output_sogs_path)
    filename = os.path.basename(output_sogs_path)
    return send_from_directory(directory, filename, as_attachment=True)

@app.route('/delete/<task_id>', methods=['DELETE'])
def delete_task_and_files_route(task_id):
    with tasks_lock:
        task_details = conversion_tasks.pop(task_id, None)

    if not task_details:
        return jsonify({"error": "Task not found"}), 404

    perform_file_deletion(task_id, task_details) # Use the helper

    return jsonify({"message": "Task and associated files processed for deletion."}), 200

def automatic_cleanup_job():
    """Periodically cleans up old completed tasks."""
    while True:
        print("Running automatic cleanup job...")
        tasks_to_delete_ids = []
        current_time = time.time()
        cleanup_age_seconds = app.config['CLEANUP_AGE_HOURS'] * 3600

        with tasks_lock:
            # Iterate over a copy of items for safe modification
            for task_id, task_details in list(conversion_tasks.items()):
                if task_details.get("status") == "completed":
                    completed_time = task_details.get("completed_at")
                    if completed_time and (current_time - completed_time > cleanup_age_seconds):
                        tasks_to_delete_ids.append(task_id)

        if tasks_to_delete_ids:
            print(f"Found {len(tasks_to_delete_ids)} tasks for automatic deletion: {tasks_to_delete_ids}")

        for task_id in tasks_to_delete_ids:
            print(f"Auto-deleting task {task_id} due to age.")
            # Get details again, then remove, then delete files
            task_details_for_deletion = None
            with tasks_lock:
                # Check if task still exists before popping, another process might have deleted it
                if task_id in conversion_tasks:
                     task_details_for_deletion = conversion_tasks.pop(task_id)

            if task_details_for_deletion: # If successfully popped
                perform_file_deletion(task_id, task_details_for_deletion)
            else:
                print(f"Task {task_id} was already removed before auto-cleanup could process it.")

        # Sleep until the next interval
        sleep_duration = app.config['CLEANUP_INTERVAL_HOURS'] * 3600
        print(f"Automatic cleanup job finished. Sleeping for {sleep_duration/3600} hours.")
        time.sleep(sleep_duration)


if __name__ == '__main__':
    # Start the cleanup thread as a daemon so it exits when the main app exits
    cleanup_thread = threading.Thread(target=automatic_cleanup_job, daemon=True)
    cleanup_thread.start()

    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)
