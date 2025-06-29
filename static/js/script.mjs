document.addEventListener('DOMContentLoaded', () => {
    const dropArea = document.getElementById('drop-area');
    const fileElem = document.getElementById('fileElem');
    const selectFolderBtn = document.getElementById('selectFolderBtn');
    const fileList = document.getElementById('file-list');
    const noActiveConversions = document.getElementById('no-active-conversions');

    // Store EventSource objects to be able to close them if needed (e.g. on delete)
    const eventSources = {};

    function updateNoActiveConversionsVisibility() {
        if (fileList.children.length === 0) {
            noActiveConversions.style.display = 'block';
        } else {
            noActiveConversions.style.display = 'none';
        }
    }
    updateNoActiveConversionsVisibility();

    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        dropArea.addEventListener(eventName, preventDefaults, false);
        document.body.addEventListener(eventName, preventDefaults, false);
    });

    function preventDefaults(e) {
        e.preventDefault();
        e.stopPropagation();
    }

    ['dragenter', 'dragover'].forEach(eventName => {
        dropArea.addEventListener(eventName, () => dropArea.classList.add('highlight'), false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
        dropArea.addEventListener(eventName, () => dropArea.classList.remove('highlight'), false);
    });

    dropArea.addEventListener('drop', handleDrop, false);
    selectFolderBtn.addEventListener('click', () => fileElem.click());
    fileElem.addEventListener('change', handleFileSelect, false);

    async function handleDrop(e) {
        const dt = e.dataTransfer;
        const items = dt.items;
        let files = [];
        if (items && items.length) {
            for (let i = 0; i < items.length; i++) {
                const item = items[i].webkitGetAsEntry();
                if (item) {
                    await processEntry(item, files, ""); // Pass initial base path as empty
                }
            }
        }
        processPlyFiles(files);
    }

    async function handleFileSelect(e) {
        const selectedFiles = e.target.files;
        let files = [];
        if (selectedFiles && selectedFiles.length) {
            for (let i = 0; i < selectedFiles.length; i++) {
                files.push(selectedFiles[i]); // webkitRelativePath is available here
            }
        }
        processPlyFiles(files);
        fileElem.value = '';
    }

    async function processEntry(entry, filesArray, currentPath) {
        if (entry.isFile) {
            await new Promise((resolve, reject) => {
                entry.file(file => {
                    // Construct webkitRelativePath for dropped files
                    const relativePath = currentPath ? `${currentPath}/${file.name}` : file.name;
                    // Create a new File object with the webkitRelativePath property
                    const fileWithRelativePath = new File([file], file.name, {type: file.type});
                    Object.defineProperty(fileWithRelativePath, 'webkitRelativePath', {
                        value: relativePath,
                        writable: false
                    });
                    filesArray.push(fileWithRelativePath);
                    resolve();
                }, err => {
                    console.error("Error getting file from entry:", err);
                    reject(err);
                });
            });
        } else if (entry.isDirectory) {
            const directoryReader = entry.createReader();
            const newPath = currentPath ? `${currentPath}/${entry.name}` : entry.name;
            await new Promise((resolve, reject) => {
                directoryReader.readEntries(async (entries) => {
                    for (const subEntry of entries) {
                        await processEntry(subEntry, filesArray, newPath);
                    }
                    resolve();
                }, err => {
                    console.error("Error reading directory entries:", err);
                    reject(err);
                });
            });
        }
    }

    function processPlyFiles(allFiles) {
        const plyFiles = allFiles.filter(file => file.name.toLowerCase().endsWith('.ply') && file.size > 0);

        if (plyFiles.length === 0) {
            alert("No .ply files found in the selection.");
            return;
        }

        plyFiles.forEach(file => {
            const fileIdentifier = file.webkitRelativePath || file.name;
            if (document.querySelector(`.file-item[data-identifier="${CSS.escape(fileIdentifier)}"]`)) {
                console.log(`File ${fileIdentifier} already in list or being processed. Skipping.`);
                return;
            }
            const uiElements = addFileToListUI(file, fileIdentifier);
            uploadFileAndListen(file, fileIdentifier, uiElements);
        });
        updateNoActiveConversionsVisibility();
    }

    function addFileToListUI(file, fileIdentifier) {
        const fileSize = (file.size / 1024 / 1024).toFixed(2) + 'MB';
        const item = document.createElement('div');
        item.classList.add('file-item');
        item.setAttribute('data-identifier', fileIdentifier);

        item.innerHTML = `
            <div class="file-info">
                <span class="file-name">${fileIdentifier}</span> - <span class="file-size">${fileSize}</span>
                <div class="progress-bar-container">
                    <div class="progress-bar" style="width: 0%;">0%</div>
                </div>
                <div class="status-message">Waiting to upload...</div>
            </div>
            <div class="file-actions">
                <button class="download-btn" style="display:none;">Download SOGS</button>
                <button class="delete-btn">Delete</button>
            </div>
        `;
        fileList.appendChild(item);
        updateNoActiveConversionsVisibility();

        const progressBar = item.querySelector('.progress-bar');
        const statusMessage = item.querySelector('.status-message');
        const downloadBtn = item.querySelector('.download-btn');
        const deleteBtn = item.querySelector('.delete-btn');

        deleteBtn.addEventListener('click', () => handleDelete(fileIdentifier, item, null)); // Task ID not known yet

        return { item, progressBar, statusMessage, downloadBtn, deleteBtn };
    }

    async function uploadFileAndListen(file, fileIdentifier, uiElements) {
        uiElements.statusMessage.textContent = 'Preparing upload...';
        const formData = new FormData();
        formData.append('file', file, file.name); // Use original filename for the 'file' part
        formData.append('relative_path', fileIdentifier); // Send the unique identifier

        try {
            uiElements.statusMessage.textContent = 'Uploading...';
            const response = await fetch('/upload', {
                method: 'POST',
                body: formData,
            });

            if (!response.ok) {
                const errorData = await response.json().catch(() => ({ detail: response.statusText }));
                throw new Error(`Upload failed: ${errorData.error || errorData.detail}`);
            }

            const result = await response.json();
            const taskId = result.task_id;

            // Update delete button to know the taskId
            uiElements.deleteBtn.onclick = () => handleDelete(fileIdentifier, uiElements.item, taskId);


            if (result.initial_status) {
                 updateFileItemUI(fileIdentifier, result.initial_status, uiElements);
            } else {
                 uiElements.statusMessage.textContent = 'Uploaded. Waiting for conversion stream...';
            }

            // Start listening for SSE updates
            listenForProgress(taskId, fileIdentifier, uiElements);

        } catch (error) {
            console.error('Upload or processing initiation error:', error);
            uiElements.statusMessage.textContent = `Error: ${error.message}`;
            if (uiElements.progressBar) uiElements.progressBar.style.width = '0%';
            if (uiElements.progressBarContainer) uiElements.progressBarContainer.style.display = 'none';
        }
    }

    function listenForProgress(taskId, fileIdentifier, uiElements) {
        if (eventSources[taskId]) {
            eventSources[taskId].close(); // Close existing if any (should not happen with unique task IDs)
        }

        const evtSource = new EventSource(`/stream/${taskId}`);
        eventSources[taskId] = evtSource; // Store it

        evtSource.onmessage = function(event) {
            const data = JSON.parse(event.data);

            if (data.error) {
                console.error("SSE Error for task", taskId, ":", data.error);
                uiElements.statusMessage.textContent = `Error: ${data.message || data.error}`;
                evtSource.close();
                delete eventSources[taskId];
                return;
            }

            // Ensure we're updating the correct item, though SSE is per task_id
            if (data.client_identifier && data.client_identifier !== fileIdentifier) {
                console.warn("SSE data for different client_identifier, ignoring for this UI element", data, fileIdentifier);
                return;
            }

            updateFileItemUI(fileIdentifier, data, uiElements);

            if (data.status === 'completed' || data.status === 'failed') {
                evtSource.close();
                delete eventSources[taskId];
                if (data.status === 'completed' && uiElements.downloadBtn && data.download_url) {
                    uiElements.downloadBtn.style.display = 'inline-block';
                    uiElements.downloadBtn.onclick = () => {
                        window.location.href = data.download_url;
                    };
                }
            }
        };

        evtSource.onerror = function(err) {
            console.error("EventSource failed for task:", taskId, err);
            uiElements.statusMessage.textContent = 'Connection error. Retrying or check server.';
            // SSE will attempt to reconnect automatically by default.
            // If we want to stop it, we can: evtSource.close(); delete eventSources[taskId];
            // For critical errors or after N retries, you might want to close it.
        };
    }

    function updateFileItemUI(fileIdentifier, data, uiElements) {
        // If uiElements are not passed, try to find them.
        // This can happen if page was reloaded and we are restoring state. (Future enhancement)
        if (!uiElements) {
            const item = document.querySelector(`.file-item[data-identifier="${CSS.escape(fileIdentifier)}"]`);
            if (!item) return; // Item not in UI
            uiElements = {
                item: item,
                progressBar: item.querySelector('.progress-bar'),
                statusMessage: item.querySelector('.status-message'),
                downloadBtn: item.querySelector('.download-btn'),
                deleteBtn: item.querySelector('.delete-btn'),
                // progressBarContainer might not exist if only addFileToListUI was called without full init
                progressBarContainer: item.querySelector('.progress-bar-container')
            };
        }

        if (uiElements.progressBarContainer) uiElements.progressBarContainer.style.display = 'block';
        if (uiElements.progressBar) {
            uiElements.progressBar.style.width = `${data.progress || 0}%`;
            uiElements.progressBar.textContent = `${data.progress || 0}%`;
        }
        if (uiElements.statusMessage) {
            uiElements.statusMessage.textContent = data.message || data.status || 'Processing...';
        }

        if (data.status === 'completed') {
            if (uiElements.downloadBtn && data.download_url) {
                uiElements.downloadBtn.style.display = 'inline-block';
                uiElements.downloadBtn.onclick = () => {
                    window.location.href = data.download_url;
                };
            }
             if (uiElements.statusMessage) uiElements.statusMessage.textContent = data.message || 'Conversion Complete!';
        } else if (data.status === 'failed') {
            if (uiElements.statusMessage) uiElements.statusMessage.textContent = `Failed: ${data.message || 'Unknown error'}`;
            if (uiElements.progressBarContainer) uiElements.progressBarContainer.style.backgroundColor = '#ffdddd'; // Indicate error
        }
    }

    async function handleDelete(fileIdentifier, itemElement, taskId) {
        // Close SSE connection if it exists for this task
        if (taskId && eventSources[taskId]) {
            eventSources[taskId].close();
            delete eventSources[taskId];
        }

        itemElement.remove();
        updateNoActiveConversionsVisibility();
        console.log(`Removed ${fileIdentifier} from list.`);

        if (taskId) { // If task ID is known, means it was at least uploaded
            try {
                const response = await fetch(`/delete/${taskId}`, { method: 'DELETE' });
                if (!response.ok) {
                    console.error(`Failed to delete task ${taskId} on server.`);
                } else {
                    console.log(`Deletion request for task ${taskId} sent to server.`);
                }
            } catch (error) {
                console.error(`Error sending delete request for task ${taskId}:`, error);
            }
        }
    }

    // Initial check for no active conversions message
    updateNoActiveConversionsVisibility();

    function loadExistingTasks() {
        const existingTasksDataElement = document.getElementById('existing-tasks-data');
        if (existingTasksDataElement && existingTasksDataElement.textContent) {
            try {
                const tasks = JSON.parse(existingTasksDataElement.textContent);
                if (tasks && tasks.length > 0) {
                    tasks.forEach(task => {
                        // We don't have the 'File' object here, so pass what's available
                        // addFileToListUI expects a file object for size, but we can adapt
                        const mockFile = {
                            name: task.original_filename,
                            size: task.file_size || 0, // Add file_size to task data if available, else 0
                            webkitRelativePath: task.client_identifier
                        };

                        // Avoid re-adding if somehow already present (e.g., dev refresh)
                        if (document.querySelector(`.file-item[data-identifier="${CSS.escape(task.client_identifier)}"]`)) {
                            console.log(`Task ${task.client_identifier} already in UI during load. Updating.`);
                             // If it exists, find its UI elements and update.
                            const item = document.querySelector(`.file-item[data-identifier="${CSS.escape(task.client_identifier)}"]`);
                            const uiElements = {
                                item: item,
                                progressBar: item.querySelector('.progress-bar'),
                                statusMessage: item.querySelector('.status-message'),
                                downloadBtn: item.querySelector('.download-btn'),
                                deleteBtn: item.querySelector('.delete-btn'),
                                progressBarContainer: item.querySelector('.progress-bar-container')
                            };
                            updateFileItemUI(task.client_identifier, task, uiElements);
                             // Attach delete handler with task ID
                            uiElements.deleteBtn.onclick = () => handleDelete(task.client_identifier, uiElements.item, task.task_id);

                            if (task.status !== 'completed' && task.status !== 'failed' && task.status !== 'error') {
                                listenForProgress(task.task_id, task.client_identifier, uiElements);
                            }

                        } else {
                            const uiElements = addFileToListUI(mockFile, task.client_identifier);
                            // Set the task_id on the delete button immediately
                            uiElements.deleteBtn.onclick = () => handleDelete(task.client_identifier, uiElements.item, task.task_id);

                            updateFileItemUI(task.client_identifier, task, uiElements);

                            if (task.status !== 'completed' && task.status !== 'failed' && task.status !== 'error') {
                                listenForProgress(task.task_id, task.client_identifier, uiElements);
                            }
                        }
                    });
                    updateNoActiveConversionsVisibility();
                }
            } catch (e) {
                console.error("Error parsing existing tasks data:", e);
            }
        }
    }

    loadExistingTasks(); // Call on initial script load
});
