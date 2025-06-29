import os
import subprocess
import uuid
import shutil
import time
import json
import threading
import queue # Added for task queue
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
task_queue = queue.Queue() # Initialize the task queue

def conversion_worker():
    print("Conversion worker thread started.")
    while True:
        try:
            task_id = task_queue.get() # Blocks until a task is available
            print(f"Worker picked up task: {task_id}")

            task_details = None
            with tasks_lock:
                if task_id in conversion_tasks:
                    task_details = conversion_tasks[task_id]
                else:
                    print(f"Worker: Task {task_id} not found in conversion_tasks. Skipping.")
                    task_queue.task_done()
                    continue

            if task_details and task_details.get("status") == "queued":
                input_ply_path = task_details.get("input_ply_path")
                output_sogs_path = task_details.get("output_sogs_path")
                original_filename = task_details.get("original_filename")

                if not all([input_ply_path, output_sogs_path, original_filename]):
                    print(f"Worker: Task {task_id} is missing critical path/name info. Setting to failed.")
                    with tasks_lock:
                        conversion_tasks[task_id]["status"] = "failed"
                        conversion_tasks[task_id]["message"] = "Internal error: Task data incomplete."
                        conversion_tasks[task_id]["updated_at"] = time.time()
                    task_queue.task_done()
                    continue

                # Actual conversion call
                run_sogs_conversion(task_id, input_ply_path, output_sogs_path, original_filename)
            else:
                # This might happen if task was deleted or status changed before worker picked it up
                print(f"Worker: Task {task_id} not in 'queued' state or details missing. Current state: {task_details.get('status') if task_details else 'N/A'}")

            task_queue.task_done() # Signal that this task is complete
            print(f"Worker finished processing task: {task_id}")

        except Exception as e:
            print(f"Error in conversion_worker: {e}")
            if 'task_id' in locals() and task_id: # Check if task_id was assigned
                try:
                    with tasks_lock:
                         if task_id in conversion_tasks: # Check if task still exists
                            conversion_tasks[task_id]["status"] = "failed"
                            conversion_tasks[task_id]["message"] = f"Worker thread error: {str(e)}"
                            conversion_tasks[task_id]["updated_at"] = time.time()
                except Exception as inner_e:
                    print(f"Error updating task status after worker error: {inner_e}")
                finally:
                    # Ensure task_done is called if a task was retrieved, even if an error occurred processing it.
                    # This prevents the queue from potentially hanging if join() were used elsewhere,
                    # and ensures the worker can pick up new tasks.
                    task_queue.task_done()


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

    # Cleanup empty directories helper
    def cleanup_empty_dirs(path_to_check, base_folder_path):
        if not path_to_check or not os.path.exists(path_to_check) or not os.path.isdir(path_to_check):
            return
        # Ensure we don't try to remove the base UPLOAD_FOLDER or OUTPUT_FOLDER itself
        # and that path_to_check is actually a subdirectory of base_folder_path
        if not path_to_check.startswith(base_folder_path + os.sep) or path_to_check == base_folder_path:
            return

        try:
            if not os.listdir(path_to_check): # Check if directory is empty
                os.rmdir(path_to_check)
                print(f"Removed empty directory: {path_to_check}")
                # Recursively try to clean up parent directory
                cleanup_empty_dirs(os.path.dirname(path_to_check), base_folder_path)
        except OSError as e:
            print(f"Error removing or checking directory {path_to_check}: {e}")

    # Cleanup for UPLOAD directory (based on client_identifier's path)
    client_identifier = task_details.get("client_identifier", "")
    upload_dir_to_check = None
    if client_identifier:
        path_dirname = os.path.dirname(client_identifier)
        if path_dirname and path_dirname != '.': # If there was a path component
            upload_relative_parts = [secure_filename(part) for part in path_dirname.split(os.sep) if part and part != '.']
            if upload_relative_parts:
                # This gets the specific subdirectory where the uploaded file was.
                upload_dir_to_check = os.path.join(UPLOAD_FOLDER, *upload_relative_parts)


    # If an input_ply_path was present, its directory is a candidate for cleanup.
    # This is more reliable if the file itself was in a subfolder of UPLOAD_FOLDER.
    if input_ply_path:
        upload_dir_to_check = os.path.dirname(input_ply_path)

    if upload_dir_to_check:
        cleanup_empty_dirs(upload_dir_to_check, UPLOAD_FOLDER)


    # Cleanup for OUTPUT directory (based on original_filename for the new structure)
    original_filename = task_details.get("original_filename")
    output_sogs_file_path = task_details.get("output_sogs_path") # This is the full path to the .sogs file

    if output_sogs_file_path: # If we have a path for the output file
        # The directory containing the .sogs file is what we want to clean up
        output_dir_to_check = os.path.dirname(output_sogs_file_path)
        if output_dir_to_check and output_dir_to_check != OUTPUT_FOLDER:
             # We expect output_dir_to_check to be something like /output/filename_base
             # After removing the .sogs file itself, if this directory is empty, remove it.
            cleanup_empty_dirs(output_dir_to_check, OUTPUT_FOLDER)
    elif original_filename: # Fallback if output_sogs_path wasn't set but we have original_filename
        output_filename_base = original_filename.rsplit('.', 1)[0]
        output_dir_to_check = os.path.join(OUTPUT_FOLDER, output_filename_base)
        cleanup_empty_dirs(output_dir_to_check, OUTPUT_FOLDER)


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
    with tasks_lock:
        # Create a serializable copy of tasks to pass to the template
        # Sort tasks by creation time, newest first, or some other sensible order
        sorted_tasks = sorted(conversion_tasks.values(), key=lambda t: t.get('created_at', 0), reverse=True)
    return render_template('index.html', existing_tasks=json.dumps(sorted_tasks))

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
        upload_relative_parts = [secure_filename(part) for part in path_dirname.split(os.sep) if part and part != '.'] if path_dirname else []

        upload_sub_dir = os.path.join(app.config['UPLOAD_FOLDER'], *upload_relative_parts)

        # New output directory structure: /output/{original_filename_without_ext}/
        output_filename_base = original_filename.rsplit('.', 1)[0]
        output_sub_dir = os.path.join(app.config['OUTPUT_FOLDER'], output_filename_base)

        os.makedirs(upload_sub_dir, exist_ok=True)
        os.makedirs(output_sub_dir, exist_ok=True) # Ensure this new output directory is created

        input_ply_path = os.path.join(upload_sub_dir, original_filename)
        output_sogs_filename = original_filename.rsplit('.', 1)[0] + '.sogs' # sogs file will have the same name as original ply
        output_sogs_path = os.path.join(output_sub_dir, output_sogs_filename) # Saved inside the new output_sub_dir

        file.save(input_ply_path)

        with tasks_lock:
            conversion_tasks[task_id] = {
                "task_id": task_id,
                "status": "queued", # Changed from "uploaded"
                "original_filename": original_filename,
                "client_identifier": relative_path,
                "input_ply_path": input_ply_path,
                "output_sogs_path": output_sogs_path,
                "progress": 0,
                "message": "File uploaded, queued for conversion.",
                "created_at": current_time,
                "updated_at": current_time,
                "file_size": file.content_length # Store file size
            }

        task_queue.put(task_id) # Add task_id to the queue
        print(f"Task {task_id} added to queue. Queue size: {task_queue.qsize()}")

        # Remove the direct thread start:
        # conversion_thread = threading.Thread(target=run_sogs_conversion, args=(task_id, input_ply_path, output_sogs_path, original_filename))
        # conversion_thread.start()

        return jsonify({
            "message": "File upload successful, conversion queued.", # Message updated
            "task_id": task_id,
            "client_identifier": relative_path,
            "initial_status": conversion_tasks[task_id] # Send the 'queued' status
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
    # Start the cleanup thread
    cleanup_thread = threading.Thread(target=automatic_cleanup_job, daemon=True)
    cleanup_thread.start()

    # Start the conversion worker thread
    worker_thread = threading.Thread(target=conversion_worker, daemon=True)
    worker_thread.start()

    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)
