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


    # Cleanup for OUTPUT directory
    # The output_sogs_path now points to the .zip file, e.g., output/filename_base.sogs.zip
    # The individual SOGS assets were in output_item_specific_dir, e.g., output/filename_base/
    # This directory should have been emptied by run_sogs_conversion after zipping.
    # So, we need to ensure this specific directory is targeted for removal if empty.

    output_item_specific_dir = task_details.get("output_item_specific_dir")
    if output_item_specific_dir and os.path.exists(output_item_specific_dir):
        print(f"Attempting cleanup of specific output item directory: {output_item_specific_dir}")
        cleanup_empty_dirs(output_item_specific_dir, OUTPUT_FOLDER)
    else:
        # Fallback if output_item_specific_dir is not set (e.g., for older task structures)
        original_filename = task_details.get("original_filename")
        if original_filename:
            output_filename_base = original_filename.rsplit('.', 1)[0]
            fallback_dir_to_check = os.path.join(OUTPUT_FOLDER, output_filename_base)
            if os.path.exists(fallback_dir_to_check):
                print(f"Attempting cleanup of fallback output item directory: {fallback_dir_to_check}")
                cleanup_empty_dirs(fallback_dir_to_check, OUTPUT_FOLDER)

    # Also, if the zip file itself was in a subdirectory (it shouldn't be with current logic, but for safety)
    # e.g. if zip was output/filename_base/filename_base.sogs.zip
    output_sogs_file_path = task_details.get("output_sogs_path")
    if output_sogs_file_path:
        zip_dir = os.path.dirname(output_sogs_file_path)
        if zip_dir and zip_dir != OUTPUT_FOLDER and os.path.exists(zip_dir):
            # This would typically be OUTPUT_FOLDER itself if zip is at output/file.zip
            # Only try to clean if it's a sub-directory of OUTPUT_FOLDER
            if zip_dir.startswith(OUTPUT_FOLDER + os.sep):
                 print(f"Attempting cleanup of zip file's directory: {zip_dir}")
                 cleanup_empty_dirs(zip_dir, OUTPUT_FOLDER)


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
                task_data["status"] = "processing_output" # Intermediate status before zipping
                task_data["progress"] = 99 # Or some other indicator that main conversion is done
                task_data["message"] = "SOGS conversion complete, packaging output..."

                # Create a ZIP file of the output directory contents
                zip_filename_base = original_filename.rsplit('.', 1)[0]
                # Place the zip file in the parent of output_dir_for_sogs, which is app.config['OUTPUT_FOLDER']
                # Or, more robustly, ensure it's in a predictable location.
                # Let's place it inside the output_dir_for_sogs for now, then adjust if needed.
                # No, better to place it one level up, in the main OUTPUT_FOLDER or its direct task-specific subfolder.
                # The output_dir_for_sogs is like 'output/filename_base/'
                # The zip should be 'output/filename_base.zip' or 'output/filename_base/filename_base.zip'

                # Let's aim for output/filename_base.zip.
                # output_sub_dir (from upload route) is 'output/filename_base'
                # output_sogs_path (original) was 'output/filename_base/filename_base.sogs'
                # output_dir_for_sogs is 'output/filename_base'

                zip_path = os.path.join(os.path.dirname(output_dir_for_sogs), f"{zip_filename_base}.sogs.zip")
                # Ensure output_dir_for_sogs contains the files to be zipped.

                try:
                    output_files = os.listdir(output_dir_for_sogs)
                    if output_files:
                        with shutil.make_archive(zip_path.replace('.zip', ''), 'zip', root_dir=output_dir_for_sogs, base_dir='.') as archive_name:
                            # shutil.make_archive adds '.zip' automatically if format is 'zip'
                            # so zip_path.replace('.zip', '') is the base name for the archive
                            # And the final archive_name will be the full path to the zip.
                            pass # The archive is created by the context manager

                        # Verify zip creation and update task data
                        if os.path.exists(zip_path):
                            print(f"Successfully created ZIP archive: {zip_path} from directory {output_dir_for_sogs}")
                            task_data["output_sogs_path"] = zip_path # This is the path to the zip file
                            task_data["message"] = "Conversion successful. Output packaged as ZIP."

                            # Clean up the individual files from output_dir_for_sogs now that they are zipped
                            print(f"Cleaning up original files from {output_dir_for_sogs} after zipping.")
                            for item in output_files:
                                item_path = os.path.join(output_dir_for_sogs, item)
                                try:
                                    if os.path.isfile(item_path) or os.path.islink(item_path):
                                        os.unlink(item_path)
                                        print(f"Deleted file: {item_path}")
                                    elif os.path.isdir(item_path): # Should not be any, but good practice
                                        shutil.rmtree(item_path)
                                        print(f"Deleted directory: {item_path}")
                                except Exception as e_clean:
                                    print(f"Error cleaning up item {item_path} after zipping: {e_clean}")
                            # perform_file_deletion will later remove the (now empty) output_dir_for_sogs
                            task_data["status"] = "completed" # Final success status
                            task_data["progress"] = 100
                            task_data["completed_at"] = time.time() # Set completion time here
                        else:
                            print(f"Error: ZIP archive {zip_path} not found after attempting creation.")
                            task_data["status"] = "failed"
                            task_data["message"] = "Conversion successful, but failed to package output."
                    else:
                        print(f"Warning: Output directory '{output_dir_for_sogs}' is empty. No ZIP created for task {task_id}.")
                        task_data["status"] = "failed"
                        task_data["message"] = "Conversion completed, but no output files were generated to package."

                except Exception as e_zip:
                    print(f"Error creating ZIP archive for task {task_id}: {e_zip}")
                    task_data["status"] = "failed"
                    task_data["message"] = f"Failed to package output: {str(e_zip)}"

                # Delete input_ply_path only if the overall process (including zipping) hasn't failed
                if task_data["status"] == "completed":
                    try:
                        os.remove(input_ply_path)
                        task_data["input_ply_path"] = None
                    except OSError as e:
                        print(f"Error deleting original file {input_ply_path}: {e}")
                        task_data["message"] += f" (Warning: Failed to delete {original_filename} after successful conversion)"
            else: # process.returncode != 0 (sogs-compress command failed)
                task_data["status"] = "failed"
                # Keep previous message if it's already an error, otherwise set a new one
                current_message = task_data.get("message", "")
                if "Error" not in current_message and "failed" not in current_message.lower():
                    task_data["message"] = f"SOGS conversion process failed. Exit code: {process.returncode}"
                # Ensure progress is not 100 if failed
                if task_data.get("progress") == 100 : task_data["progress"] = 99

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
        tasks_list = []
        for task_data_orig in conversion_tasks.values():
            task_data = task_data_orig.copy() # Work on a copy
            if task_data.get("status") == "completed":
                # Ensure download_url is present for completed tasks when loading the page
                task_data["download_url"] = f"/download/{task_data['task_id']}"
            tasks_list.append(task_data)

        # Sort tasks by creation time, newest first, or some other sensible order
        sorted_tasks = sorted(tasks_list, key=lambda t: t.get('created_at', 0), reverse=True)
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
                "output_sogs_path": output_sogs_path, # This will be the initial expectation, updated by conversion
                "output_item_specific_dir": output_sub_dir, # Path like 'output/filename_base'
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
                    if completed_time:
                        age_seconds = current_time - completed_time
                        if age_seconds > cleanup_age_seconds:
                            print(f"Task {task_id} marked for deletion: completed {age_seconds/3600:.2f} hours ago (threshold: {cleanup_age_seconds/3600:.2f} hours).")
                            tasks_to_delete_ids.append(task_id)
                        # else:
                        #     print(f"Task {task_id} is completed but not old enough for deletion: {age_seconds/3600:.2f} hours old.")
                    # else:
                    #     print(f"Task {task_id} is completed but has no completed_at time.")


        if tasks_to_delete_ids:
            print(f"Tasks to be auto-deleted by ID: {tasks_to_delete_ids}")

        for task_id in tasks_to_delete_ids:
            # Retrieve task details again to ensure we have the latest, though pop should be atomic for the entry itself.
            # The main reason is to pass it to perform_file_deletion.
            task_details_for_deletion = None # Defined outside lock
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
