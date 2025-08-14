import cv2
import pandas as pd
import os
import glob
from datetime import datetime, timedelta
import argparse
from tqdm import tqdm
import numpy as np

class PostProcessor:
    """
    Post-processing tool to segment videos based on gesture labels for model training.
    
    This script takes the video feeds from each camera and segments them based on
    gesture timestamps from the auto_labels CSV file. Each gesture segment is saved
    as a separate video file with appropriate labeling, optimized for model training.
    
    Key features for model training:
    - Removes first 5 seconds (reading time, not performance time)
    - Ensures consistent video quality and format
    - Creates standardized naming convention
    - Organizes by participant and gesture type
    - Extracts frames at 30 FPS for further processing
    """
    
    def __init__(self, participant_id, base_path="dataset"):
        self.participant_id = participant_id
        self.base_path = base_path
        self.video_path = os.path.join(base_path, "images", str(participant_id))
        self.log_path = os.path.join(base_path, "logs", str(participant_id))
        self.output_path = os.path.join(base_path, "post-processed", str(participant_id))
        self.frames_path = os.path.join(base_path, "frames", str(participant_id))
        
        # Create output directories
        os.makedirs(self.output_path, exist_ok=True)
        os.makedirs(self.frames_path, exist_ok=True)
        
        # Load gesture labels
        self.gesture_labels = self._load_gesture_labels()
        
        # Camera configuration - exclude camera 6 as requested
        self.cameras = self._get_camera_list()
        
        # Model training parameters
        self.reading_time_cutoff = 5  # Cut first 5 seconds (reading time)
        self.min_segment_duration = 3  # Minimum segment duration for training
        self.target_fps = 24  # Standardize to 24 FPS for training (matching camera settings)
        self.frame_extraction_fps = 24  # Extract frames at 24 FPS (matching camera settings)
    
    def _load_gesture_labels(self):
        """Load gesture labels from auto_labels CSV file."""
        gesture_file = os.path.join("logs", f"auto_labels_{self.participant_id}.csv")
        
        if not os.path.exists(gesture_file):
            raise FileNotFoundError(f"Gesture labels file not found: {gesture_file}")
        
        df = pd.read_csv(gesture_file)
        df['Timestamp'] = pd.to_datetime(df['Timestamp'])
        
        # Fix 1-hour time difference by adding 1 hour to gesture timestamps
        # This aligns gesture timestamps with camera recording times
        df['Timestamp'] = df['Timestamp'] + timedelta(hours=1)
        
        # Convert to timezone-naive timestamps for consistent comparison
        df['Timestamp'] = df['Timestamp'].dt.tz_localize(None)
        
        # Sort by timestamp
        df = df.sort_values('Timestamp').reset_index(drop=True)
        
        print(f"Loaded {len(df)} gesture labels for participant {self.participant_id}")
        print(f"Adjusted timestamps by +1 hour to match camera recordings")
        return df
    
    def _get_camera_list(self):
        """Get list of available cameras for this participant."""
        # Look for webcam video files
        video_files = glob.glob(os.path.join(self.video_path, "*/webcam_*.mp4"))
        cameras = []
        
        for video_file in video_files:
            # Extract camera index from filename
            filename = os.path.basename(video_file)
            if filename.startswith("webcam_") and filename.endswith(".mp4"):
                cam_index = filename.replace("webcam_", "").replace(".mp4", "")
                if cam_index.isdigit():
                    cam_num = int(cam_index)
                    # Exclude camera 6 as requested
                    if cam_num != 6:
                        cameras.append(cam_num)
        
        # Also check for Azure Kinect - treat as three separate cameras
        azure_path = os.path.join(self.video_path, "azure")
        if os.path.exists(azure_path):
            # Check for all three Azure Kinect video types
            azure_files = glob.glob(os.path.join(azure_path, "webcam_azure_kinect_*.mp4"))
            if azure_files:
                # Add each Azure Kinect video type as a separate camera
                if os.path.exists(os.path.join(azure_path, "webcam_azure_kinect_color.mp4")):
                    cameras.append("azure_color")
                if os.path.exists(os.path.join(azure_path, "webcam_azure_kinect_depth.mp4")):
                    cameras.append("azure_depth")
                if os.path.exists(os.path.join(azure_path, "webcam_azure_kinect_ir.mp4")):
                    cameras.append("azure_ir")
        
        print(f"Found cameras (excluding camera 6): {cameras}")
        
        # Sort cameras: integers first (numerically), then strings (alphabetically)
        def camera_sort_key(cam):
            if isinstance(cam, int):
                return (0, cam)  # Type 0 for integers, with integer value
            else:
                return (1, str(cam))  # Type 1 for strings, with string value
        
        return sorted(cameras, key=camera_sort_key)
    
    def _get_frame_timestamps(self, camera_id):
        """Get frame timestamps for a specific camera."""
        if str(camera_id).startswith("azure_"):
            # All Azure Kinect video types use the same timestamp CSV
            csv_file = os.path.join(self.log_path, "webcam_azure_kinect.csv")
        else:
            csv_file = os.path.join(self.log_path, f"webcam_{camera_id}.csv")
        
        if not os.path.exists(csv_file):
            print(f"Warning: CSV file not found for camera {camera_id}: {csv_file}")
            return pd.DataFrame()
        
        df = pd.read_csv(csv_file)
        df['Timestamp'] = pd.to_datetime(df['Timestamp'])
        
        # Convert to timezone-naive timestamps for consistent comparison
        df['Timestamp'] = df['Timestamp'].dt.tz_localize(None)
        
        return df
    
    def _get_video_path(self, camera_id):
        """Get video file path for a specific camera."""
        if camera_id == "azure_color":
            return os.path.join(self.video_path, "azure", "webcam_azure_kinect_color.mp4")
        elif camera_id == "azure_depth":
            return os.path.join(self.video_path, "azure", "webcam_azure_kinect_depth.mp4")
        elif camera_id == "azure_ir":
            return os.path.join(self.video_path, "azure", "webcam_azure_kinect_ir.mp4")
        else:
            return os.path.join(self.video_path, str(camera_id), f"webcam_{camera_id}.mp4")
    
    def _find_gesture_segments(self, segment_duration=15):
        """
        Find gesture segments with fixed 15-second duration starting at each gesture.
        Excludes segments that start within the first 5 seconds of camera recording (reading time).
        
        Args:
            segment_duration: Duration of each segment in seconds (default: 15)
        
        Returns:
            List of dictionaries with segment info
        """
        segments = []
        
        # Get camera recording start time from the first available camera
        camera_start_time = self._get_camera_start_time()
        
        print(f"Camera recording started at: {camera_start_time}")
        print(f"Reading time cutoff: {camera_start_time + timedelta(seconds=self.reading_time_cutoff)}")
        
        for idx, row in self.gesture_labels.iterrows():
            gesture_time = row['Timestamp']
            gesture_name = row['Gesture']
            gesture_index = row['Gesture_Index']
            
            # Create 15-second window starting at gesture timestamp
            start_time = gesture_time
            end_time = gesture_time + timedelta(seconds=segment_duration)
            
            # Skip segments that start within the first 5 seconds of camera recording (reading time)
            if start_time < camera_start_time + timedelta(seconds=self.reading_time_cutoff):
                print(f"Skipping segment {idx}: starts during reading time (camera started at {camera_start_time})")
                continue
            
            # Check if this segment overlaps with previous one
            if segments and segments[-1]['end_time'] > start_time:
                # Extend previous segment to include this gesture
                segments[-1]['end_time'] = end_time
                segments[-1]['gestures'].append({
                    'name': gesture_name,
                    'index': gesture_index,
                    'time': gesture_time
                })
            else:
                # Create new segment
                segments.append({
                    'start_time': start_time,
                    'end_time': end_time,
                    'gestures': [{
                        'name': gesture_name,
                        'index': gesture_index,
                        'time': gesture_time
                    }]
                })
        
        # Filter out segments that are too short for training
        filtered_segments = []
        for segment in segments:
            duration = (segment['end_time'] - segment['start_time']).total_seconds()
            if duration >= self.min_segment_duration:
                filtered_segments.append(segment)
            else:
                print(f"Skipping segment: too short ({duration:.1f}s < {self.min_segment_duration}s)")
        
        print(f"Created {len(filtered_segments)} training-ready video segments (15 seconds each)")
        return filtered_segments
    
    def _extract_video_segment(self, video_path, start_time, end_time, output_path, 
                              frame_timestamps, fps=30):
        """
        Extract a video segment based on timestamps, optimized for model training.
        Records exactly 15 seconds starting from gesture timestamp.
        
        Args:
            video_path: Path to input video file
            start_time: Start timestamp
            end_time: End timestamp
            output_path: Path for output video segment
            frame_timestamps: DataFrame with frame timestamps
            fps: Video frame rate
        """
        if not os.path.exists(video_path):
            print(f"Warning: Video file not found: {video_path}")
            return False
        
        # Find frame indices for the time window
        mask = (frame_timestamps['Timestamp'] >= start_time) & (frame_timestamps['Timestamp'] <= end_time)
        segment_frames = frame_timestamps[mask]
        
        if len(segment_frames) == 0:
            print(f"No frames found in time window {start_time} to {end_time}")
            return False
        
        # Open video
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print(f"Could not open video: {video_path}")
            return False
        
        # Get video properties
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        
        # Use target FPS for consistent training data
        output_fps = self.target_fps
        
        # Create video writer with standardized format for training
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(output_path, fourcc, output_fps, (width, height))
        
        # Find starting frame index
        start_frame_idx = segment_frames.index[0]
        
        # Set position to start frame
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame_idx)
        
        # Calculate exact number of frames needed for 15 seconds
        target_frames = int(15 * output_fps)  # 15 seconds × 24 FPS = 360 frames
        
        # Extract exactly target_frames frames for consistent training data
        frame_count = 0
        frame_interval = actual_fps / output_fps if actual_fps != output_fps else 1
        frame_counter = 0
        frames_written = 0
        
        for _ in range(target_frames):  # Extract exactly 450 frames for 15 seconds
            ret, frame = cap.read()
            if not ret:
                break
            
            # Frame rate conversion: only write every nth frame if needed
            if frame_counter % frame_interval < 1:
                out.write(frame)
                frames_written += 1
            
            frame_counter += 1
        
        cap.release()
        out.release()
        
        print(f"Extracted {frames_written} frames to {output_path} (FPS: {output_fps})")
        return True
    
    def _get_camera_start_time(self):
        """
        Get the start time of camera recording from the first available camera.
        
        Returns:
            datetime: Start time of camera recording
        """
        for camera_id in self.cameras:
            frame_timestamps = self._get_frame_timestamps(camera_id)
            if not frame_timestamps.empty:
                return frame_timestamps['Timestamp'].iloc[0]
        
        # Fallback: return first gesture time if no camera data available
        return self.gesture_labels['Timestamp'].iloc[0]
    
    def _get_segment_start_time(self, segment_name):
        """
        Extract the start time for a segment based on its name.
        
        Args:
            segment_name: Segment name (e.g., 'p0_camera1_seg000')
        
        Returns:
            datetime: Start time of the segment
        """
        # Parse segment index from name (e.g., 'p0_camera1_seg000' -> 0)
        if '_seg' in segment_name:
            seg_part = segment_name.split('_seg')[1]
            if seg_part.isdigit():
                segment_index = int(seg_part)
                
                # Get the segments that were created during video processing
                segments = self._find_gesture_segments(15)  # 15 second segments
                
                if segment_index < len(segments):
                    return segments[segment_index]['start_time']
        
        # Fallback: return current time if we can't determine
        print(f"Warning: Could not determine start time for segment {segment_name}, using current time")
        return datetime.now()
    
    def _get_gesture_label_for_frame(self, frame_timestamp):
        """
        Get the gesture label for a specific frame timestamp.
        
        Args:
            frame_timestamp: Timestamp of the frame
        
        Returns:
            str: Gesture label for the frame, or 'no_gesture' if no gesture at that time
        """
        # Find the gesture that was active at this frame's timestamp
        for idx, row in self.gesture_labels.iterrows():
            gesture_time = row['Timestamp']
            gesture_name = row['Gesture']
            
            # Check if this frame falls within the gesture window
            # We'll use a 15-second window starting from the gesture timestamp
            gesture_start = gesture_time
            gesture_end = gesture_time + timedelta(seconds=15)
            
            if gesture_start <= frame_timestamp <= gesture_end:
                return gesture_name
        
        # If no gesture found, return 'no_gesture'
        return 'no_gesture'
    
    def _load_azure_skeletal_data(self):
        """
        Load Azure Kinect skeletal tracking data from CSV file.
        
        Returns:
            dict: Dictionary mapping timestamps to skeletal data
        """
        azure_csv_path = os.path.join(self.log_path, "webcam_azure_kinect.csv")
        if not os.path.exists(azure_csv_path):
            print(f"Warning: Azure Kinect CSV not found: {azure_csv_path}")
            return {}
        
        skeletal_data = {}
        
        try:
            with open(azure_csv_path, 'r') as f:
                for line_num, line in enumerate(f):
                    if line_num == 0:  # Skip header
                        continue
                    
                    # Parse the line: timestamp, c_success, ir_success, d_success, [joint_data...]
                    parts = line.strip().split(',')
                    if len(parts) < 4:
                        continue
                    
                    timestamp_str = parts[0]
                    try:
                        timestamp = pd.to_datetime(timestamp_str)
                        # Convert to timezone-naive for consistency
                        timestamp = timestamp.tz_localize(None)
                    except:
                        continue
                    
                    # Parse joint data (each joint has x,y,z,confidence in format [x y z confidence])
                    joint_data = {}
                    joint_confidence = {}  # Store confidence values separately
                    joint_names = [
                        'neck', 'nose', 'pelvis', 'wrist-left', 'wrist-right', 'elbow-left', 'elbow-right',
                        'thumb-left', 'thumb-right', 'ear-left', 'ear-right', 'head', 'clavicle-left', 'clavicle-right',
                        'eye-left', 'eye-right', 'hand-left', 'hand-right', 'handtip-left', 'handtip-right',
                        'foot-left', 'foot-right', 'ankle-right', 'ankle-left', 'hip-left', 'hip-right',
                        'shoulder-left', 'shoulder-right', 'spine-chest', 'spine-navel', 'knee-left', 'knee-right'
                    ]
                    
                    # Start from index 4 (after timestamp and boolean flags)
                    joint_start_idx = 4
                    for i, joint_name in enumerate(joint_names):
                        if joint_start_idx + i < len(parts):
                            try:
                                # Extract the joint data string: [x y z confidence]
                                joint_str = parts[joint_start_idx + i].strip()
                                
                                # Remove brackets and split by spaces
                                joint_str = joint_str.strip('[]')
                                values = joint_str.split()
                                
                                if len(values) >= 4:  # Now checking for 4 values: x, y, z, confidence
                                    x = float(values[0])
                                    y = float(values[1])
                                    z = float(values[2])
                                    confidence = float(values[3])
                                    
                                    joint_data[joint_name] = f"{x:.3f},{y:.3f},{z:.3f}"
                                    joint_confidence[joint_name] = confidence
                                elif len(values) >= 3:  # Fallback for 3 values
                                    x = float(values[0])
                                    y = float(values[1])
                                    z = float(values[2])
                                    
                                    joint_data[joint_name] = f"{x:.3f},{y:.3f},{z:.3f}"
                                    joint_confidence[joint_name] = 0.0  # Default confidence
                                else:
                                    joint_data[joint_name] = "0.000,0.000,0.000"
                                    joint_confidence[joint_name] = 0.0
                            except (ValueError, IndexError):
                                joint_data[joint_name] = "0.000,0.000,0.000"  # Default if parsing fails
                                joint_confidence[joint_name] = 0.0
                        else:
                            joint_data[joint_name] = "0.000,0.000,0.000"  # Default if not enough data
                            joint_confidence[joint_name] = 0.0
                    
                    # Store both joint data and confidence data
                    skeletal_data[timestamp] = {
                        'joints': joint_data,
                        'confidence': joint_confidence
                    }
            
            print(f"Loaded skeletal data for {len(skeletal_data)} timestamps from Azure Kinect")
            
            # Debug: Show sample of parsed data
            if skeletal_data:
                sample_timestamp = list(skeletal_data.keys())[0]
                sample_data = skeletal_data[sample_timestamp]
                print(f"Sample skeletal data from {sample_timestamp}:")
                print(f"  Joints: {len(sample_data['joints'])} body parts")
                print(f"  Confidence: {len(sample_data['confidence'])} confidence values")
                # Show first 5 joints with their confidence
                for i, joint_name in enumerate(list(sample_data['joints'].keys())[:5]):
                    coords = sample_data['joints'][joint_name]
                    conf = sample_data['confidence'][joint_name]
                    print(f"  {joint_name}: {coords} (confidence: {conf})")
            
            return skeletal_data
            
        except Exception as e:
            print(f"Error loading Azure Kinect skeletal data: {e}")
            return {}
    
    def _get_skeletal_data_for_frame(self, frame_timestamp, skeletal_data):
        """
        Get skeletal data for a specific frame timestamp.
        
        Args:
            frame_timestamp: Timestamp of the frame
            skeletal_data: Dictionary of skeletal data by timestamp
        
        Returns:
            dict: Skeletal data for the frame, or empty dict if not found
        """
        if not skeletal_data:
            return {}
        
        # Find the closest timestamp within a reasonable window (e.g., 100ms)
        closest_timestamp = None
        min_time_diff = timedelta(milliseconds=100)
        
        for timestamp in skeletal_data.keys():
            time_diff = abs(timestamp - frame_timestamp)
            if time_diff < min_time_diff:
                min_time_diff = time_diff
                closest_timestamp = timestamp
        
        if closest_timestamp:
            return skeletal_data[closest_timestamp]
        
        return {'joints': {}, 'confidence': {}}
    
    def _extract_frames_from_video(self, video_path, output_dir, segment_name, segment_start_time, frame_data_list, skeletal_data=None):
        """
        Extract frames from a video segment at 30 FPS.
        
        Args:
            video_path: Path to the video segment
            output_dir: Directory to save frames (camera folder)
            segment_name: Name of the segment for frame naming
            segment_start_time: Start timestamp of the segment
            frame_data_list: List to append frame data (filename, timestamp) for CSV
            skeletal_data: Optional skeletal data for Azure Kinect cameras
        
        Returns:
            Number of frames extracted
        """
        if not os.path.exists(video_path):
            print(f"Warning: Video file not found: {video_path}")
            return 0
        
        # Open video
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print(f"Could not open video: {video_path}")
            return 0
        
        # Get video properties
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        
        # Calculate frame interval for 30 FPS extraction
        frame_interval = max(1, int(actual_fps / self.frame_extraction_fps))
        
        frame_count = 0
        frames_extracted = 0
        
        print(f"Extracting frames from {video_path} (target: {self.frame_extraction_fps} FPS)")
        
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            
            # Extract every nth frame to achieve target FPS
            if frame_count % frame_interval == 0:
                # Calculate timestamp for this frame
                frame_time_offset = frames_extracted / self.frame_extraction_fps  # seconds from segment start
                frame_timestamp = segment_start_time + timedelta(seconds=frame_time_offset)
                
                # Get gesture label for this frame
                gesture_label = self._get_gesture_label_for_frame(frame_timestamp)
                
                # Get skeletal data for Azure Kinect cameras
                frame_skeletal_data = {}
                if skeletal_data:
                    frame_skeletal_data = self._get_skeletal_data_for_frame(frame_timestamp, skeletal_data)
                
                # Create simple frame filename without timestamp
                frame_filename = f"{segment_name}_frame_{frames_extracted:06d}.jpg"
                frame_path = os.path.join(output_dir, frame_filename)
                
                # Save frame as JPEG
                cv2.imwrite(frame_path, frame)
                
                # Store frame data for CSV
                frame_data = {
                    'filename': frame_filename,
                    'timestamp': frame_timestamp,
                    'segment': segment_name,
                    'frame_number': frames_extracted,
                    'camera_id': os.path.basename(output_dir).replace('camera_', ''),
                    'gesture_label': gesture_label
                }
                
                # Add skeletal data columns for Azure Kinect cameras
                if frame_skeletal_data and 'joints' in frame_skeletal_data:
                    for joint_name, coordinates in frame_skeletal_data['joints'].items():
                        frame_data[f'skeletal_{joint_name}'] = coordinates
                    
                    # Add confidence data columns
                    if 'confidence' in frame_skeletal_data:
                        for joint_name, confidence in frame_skeletal_data['confidence'].items():
                            frame_data[f'confidence_{joint_name}'] = confidence
                
                frame_data_list.append(frame_data)
                
                frames_extracted += 1
            
            frame_count += 1
        
        cap.release()
        print(f"Extracted {frames_extracted} frames to {output_dir}")
        return frames_extracted
    
    def _create_frame_timestamps_csv(self, frame_data_list):
        """
        Create a CSV file mapping each frame to its timestamp and metadata.
        
        Args:
            frame_data_list: List of dictionaries containing frame information
        """
        if not frame_data_list:
            print("No frame data to save to CSV")
            return
        
        # Convert to DataFrame for easier manipulation
        df = pd.DataFrame(frame_data_list)
        
        # Add participant ID column
        df['participant_id'] = self.participant_id
        
        # Sort by timestamp for better organization
        df = df.sort_values(['camera_id', 'timestamp']).reset_index(drop=True)
        
        # Format timestamp for better readability
        df['timestamp_formatted'] = df['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S.%f').str[:-3]
        
        # Get all columns and organize them logically
        all_columns = list(df.columns)
        
        # Core columns (always present)
        core_columns = ['filename', 'participant_id', 'camera_id', 'timestamp_formatted', 'gesture_label']
        
        # Other columns (segment, frame_number, timestamp)
        other_columns = [col for col in all_columns if col not in core_columns and not col.startswith('skeletal_')]
        
        # Skeletal columns (if present) - put at the end
        skeletal_columns = [col for col in all_columns if col.startswith('skeletal_')]
        skeletal_columns.sort()  # Sort alphabetically
        
        # Confidence columns (if present) - put after skeletal
        confidence_columns = [col for col in all_columns if col.startswith('confidence_')]
        confidence_columns.sort()  # Sort alphabetically
        
        # Final column order: core -> other -> skeletal -> confidence
        columns_order = core_columns + other_columns + skeletal_columns + confidence_columns
        
        # Reorder DataFrame
        df = df[columns_order]
        
        # Save to CSV
        csv_path = os.path.join(self.frames_path, 'frame_timestamps.csv')
        df.to_csv(csv_path, index=False)
        
        print(f"Created frame timestamps CSV with {len(df)} entries")
        print(f"CSV columns: {', '.join(columns_order)}")
        
        # Show sample of the data
        print("\nSample frame data:")
        print(df.head().to_string(index=False))
        
        # Show gesture distribution
        gesture_counts = df['gesture_label'].value_counts()
        print(f"\nGesture distribution in frames:")
        for gesture, count in gesture_counts.items():
            print(f"  {gesture}: {count} frames")
        
        # Show skeletal data information
        if skeletal_columns:
            print(f"\nSkeletal tracking data included for {len(skeletal_columns)} body parts:")
            print(f"  Body parts: {', '.join([col.replace('skeletal_', '') for col in skeletal_columns[:5]])}...")
            print(f"  Format: x,y,z coordinates (e.g., '123.456,-78.901,234.567')")
            print(f"  Available for Azure Kinect cameras only")
        
        # Show confidence data information
        if confidence_columns:
            print(f"\nConfidence data included for {len(confidence_columns)} body parts:")
            print(f"  Body parts: {', '.join([col.replace('confidence_', '') for col in confidence_columns[:5]])}...")
            print(f"  Format: confidence values (0=none, 1=low, 2=medium, 3=high)")
            print(f"  Available for Azure Kinect cameras only")
    
    def _create_training_csv(self, frame_data_list):
        """
        Create a training-ready CSV where each timestamp appears once with all camera frames aligned.
        Each camera becomes a column showing the frame filename for that timestamp.
        
        Args:
            frame_data_list: List of dictionaries containing frame information
        """
        if not frame_data_list:
            print("No frame data to create training CSV")
            return
        
        # Convert to DataFrame for easier manipulation
        df = pd.DataFrame(frame_data_list)
        
        # Add participant ID column
        df['participant_id'] = self.participant_id
        
        # Format timestamp for better readability
        df['timestamp_formatted'] = df['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S.%f').str[:-3]
        
        # Get unique timestamps and sort them chronologically
        unique_timestamps = sorted(df['timestamp_formatted'].unique())
        
        # Create new training DataFrame
        training_data = []
        
        for timestamp in unique_timestamps:
            # Get all frames for this timestamp
            timestamp_frames = df[df['timestamp_formatted'] == timestamp]
            
            # Start with basic info (use first row since timestamp should be same)
            first_frame = timestamp_frames.iloc[0]
            row_data = {
                'timestamp': timestamp,
                'participant_id': first_frame['participant_id'],
                'gesture_label': first_frame['gesture_label']
            }
            
            # Add camera columns - each camera gets its own column
            for camera_id in self.cameras:
                camera_frames = timestamp_frames[timestamp_frames['camera_id'] == str(camera_id)]
                if not camera_frames.empty:
                    # Get the frame filename for this camera at this timestamp
                    frame_filename = camera_frames.iloc[0]['filename']
                    # Create full file path
                    full_path = os.path.join(self.frames_path, f"camera_{camera_id}", frame_filename)
                    # Use forward slashes for cross-platform compatibility
                    full_path = full_path.replace('\\', '/')
                    row_data[f'camera_{camera_id}'] = full_path
                else:
                    # No frame for this camera at this timestamp
                    row_data[f'camera_{camera_id}'] = ''
            
            # Add skeletal data columns (if available)
            # Use the first available frame with skeletal data
            skeletal_frames = timestamp_frames[timestamp_frames['camera_id'].str.startswith('azure_')]
            if not skeletal_frames.empty:
                skeletal_frame = skeletal_frames.iloc[0]
                for col in df.columns:
                    if col.startswith('skeletal_'):
                        row_data[col] = skeletal_frame[col]
                    elif col.startswith('confidence_'):
                        row_data[col] = skeletal_frame[col]
            else:
                # No skeletal data available for this timestamp
                for col in df.columns:
                    if col.startswith('skeletal_') or col.startswith('confidence_'):
                        row_data[col] = ''
            
            training_data.append(row_data)
        
        # Convert to DataFrame
        training_df = pd.DataFrame(training_data)
        
        # Organize columns logically
        all_columns = list(training_df.columns)
        
        # Core columns
        core_columns = ['timestamp', 'participant_id', 'gesture_label']
        
        # Camera columns
        camera_columns = [col for col in all_columns if col.startswith('camera_')]
        camera_columns.sort()  # Sort alphabetically
        
        # Skeletal columns
        skeletal_columns = [col for col in all_columns if col.startswith('skeletal_')]
        skeletal_columns.sort()  # Sort alphabetically
        
        # Confidence columns
        confidence_columns = [col for col in all_columns if col.startswith('confidence_')]
        confidence_columns.sort()  # Sort alphabetically
        
        # Final column order
        columns_order = core_columns + camera_columns + skeletal_columns + confidence_columns
        training_df = training_df[columns_order]
        
        # Save training CSV
        training_csv_path = os.path.join(self.frames_path, 'training_aligned.csv')
        training_df.to_csv(training_csv_path, index=False)
        
        print(f"\nCreated training CSV with {len(training_df)} unique timestamps")
        print(f"Training CSV saved to: {training_csv_path}")
        print(f"CSV columns: {', '.join(columns_order)}")
        
        # Show sample of the training data
        print("\nSample training data:")
        print(training_df.head().to_string(index=False))
        
        # Show confidence data information if available
        if confidence_columns:
            print(f"\nConfidence data included for {len(confidence_columns)} body parts:")
            print(f"  Body parts: {', '.join([col.replace('confidence_', '') for col in confidence_columns[:5]])}...")
            print(f"  Format: confidence values (0=none, 1=low, 2=medium, 3=high)")
            print(f"  Available for Azure Kinect cameras only")
        
        # Show camera coverage statistics
        print(f"\nCamera coverage statistics:")
        for camera_id in self.cameras:
            camera_col = f'camera_{camera_id}'
            if camera_col in training_df.columns:
                frames_count = training_df[camera_col].str.len() > 0
                coverage = frames_count.sum()
                total = len(training_df)
                print(f"  Camera {camera_id}: {coverage}/{total} timestamps ({coverage/total*100:.1f}%)")
        
        return training_df
    
    def extract_frames_from_segments(self):
        """
        Extract frames from all segmented videos at 30 FPS.
        Creates a frames folder structure with separate folders for each camera.
        All frames from all segments are stored directly in the camera folder.
        Creates a CSV file mapping each frame to its timestamp.
        """
        print(f"\nExtracting frames from segmented videos...")
        print(f"Frames will be saved to: {self.frames_path}")
        
        total_frames = 0
        all_frame_data = []  # Collect all frame data for CSV
        
        # Load Azure Kinect skeletal data if available
        azure_skeletal_data = self._load_azure_skeletal_data()
        
        # Process each camera
        for camera_id in tqdm(self.cameras, desc="Extracting frames from cameras"):
            print(f"\nProcessing frames for camera {camera_id}...")
            
            # Create camera frames directory
            camera_frames_dir = os.path.join(self.frames_path, f"camera_{camera_id}")
            os.makedirs(camera_frames_dir, exist_ok=True)
            
            # Get camera output directory (where segmented videos are stored)
            camera_output_dir = os.path.join(self.output_path, f"camera_{camera_id}")
            
            if not os.path.exists(camera_output_dir):
                print(f"Warning: No segmented videos found for camera {camera_id}")
                continue
            
            # Get all segmented video files for this camera
            video_segments = glob.glob(os.path.join(camera_output_dir, "*.mp4"))
            
            if not video_segments:
                print(f"No video segments found for camera {camera_id}")
                continue
            
            print(f"Found {len(video_segments)} video segments for camera {camera_id}")
            
            # Determine if this is an Azure Kinect camera
            is_azure_camera = str(camera_id).startswith('azure_')
            
            # Extract frames from each segment
            camera_total_frames = 0
            for video_segment in video_segments:
                # Get segment name from filename (without extension)
                segment_name = os.path.splitext(os.path.basename(video_segment))[0]
                
                # Extract segment start time from the segment name
                segment_start_time = self._get_segment_start_time(segment_name)
                
                # Extract frames and collect frame data
                frames_count = self._extract_frames_from_video(
                    video_segment, 
                    camera_frames_dir, 
                    segment_name,
                    segment_start_time,
                    all_frame_data,
                    azure_skeletal_data if is_azure_camera else None
                )
                
                camera_total_frames += frames_count
            
            print(f"Camera {camera_id}: Total frames extracted: {camera_total_frames}")
            total_frames += camera_total_frames
        
        # Create CSV file with all frame data
        if all_frame_data:
            self._create_frame_timestamps_csv(all_frame_data)
            # Also create training-ready CSV
            self._create_training_csv(all_frame_data)
        
        print(f"\nFrame extraction complete!")
        print(f"Total frames extracted: {total_frames}")
        print(f"Frames saved to: {self.frames_path}")
        print(f"Frame timestamps CSV created at: {os.path.join(self.frames_path, 'frame_timestamps.csv')}")
        print(f"Training CSV created at: {os.path.join(self.frames_path, 'training_aligned.csv')}")
        
        return total_frames

    def process_videos(self, segment_duration=15):
        """
        Process all camera videos based on gesture labels for model training.
        
        Args:
            segment_duration: Duration of each segment in seconds (default: 15)
        """
        # Get gesture segments
        segments = self._find_gesture_segments(segment_duration)
        
        if not segments:
            print("No valid segments found for training. Check gesture timestamps and reading time cutoff.")
            return
        
        # Process each camera
        for camera_id in tqdm(self.cameras, desc="Processing cameras"):
            print(f"\nProcessing camera {camera_id}...")
            
            # Get video and timestamp data
            video_path = self._get_video_path(camera_id)
            frame_timestamps = self._get_frame_timestamps(camera_id)
            
            if frame_timestamps.empty:
                print(f"Skipping camera {camera_id} - no timestamp data")
                continue
            
            # Create camera output directory
            camera_output_dir = os.path.join(self.output_path, f"camera_{camera_id}")
            os.makedirs(camera_output_dir, exist_ok=True)
            
            # Process each segment
            for seg_idx, segment in enumerate(segments):
                # Create segment filename with training-friendly naming
                segment_filename = f"p{self.participant_id}_camera{camera_id}_seg{seg_idx:03d}.mp4"
                output_path = os.path.join(camera_output_dir, segment_filename)
                
                # Extract video segment
                success = self._extract_video_segment(
                    video_path, 
                    segment['start_time'], 
                    segment['end_time'], 
                    output_path, 
                    frame_timestamps
                )
                
                if success:
                    print(f"Created training segment {seg_idx}")
    
    def process_videos_and_frames(self, segment_duration=15):
        """
        Process videos and extract frames in one complete workflow.
        
        Args:
            segment_duration: Duration of each segment in seconds (default: 15)
        """
        print("="*60)
        print("STEP 1: Video Segmentation")
        print("="*60)
        
        # First, segment the videos
        self.process_videos(segment_duration)
        
        print("\n" + "="*60)
        print("STEP 2: Frame Extraction")
        print("="*60)
        
        # Then, extract frames from the segmented videos
        total_frames = self.extract_frames_from_segments()
        
        print("\n" + "="*60)
        print("PROCESSING COMPLETE!")
        print("="*60)
        print(f"Videos segmented and saved to: {self.output_path}")
        print(f"Frames extracted and saved to: {self.frames_path}")
        print(f"Total frames extracted: {total_frames}")

    def get_processing_statistics(self):
        """Print statistics about the processing for training."""
        if not os.path.exists(self.output_path):
            print("No processed videos found. Run process_videos() first.")
            return
        
        # Count segments per camera
        for camera_id in self.cameras:
            camera_dir = os.path.join(self.output_path, f"camera_{camera_id}")
            if os.path.exists(camera_dir):
                segments = glob.glob(os.path.join(camera_dir, "*.mp4"))
                print(f"Camera {camera_id}: {len(segments)} training segments")
        
        # Count frames per camera
        if os.path.exists(self.frames_path):
            print(f"\nFrame extraction statistics:")
            for camera_id in self.cameras:
                camera_frames_dir = os.path.join(self.frames_path, f"camera_{camera_id}")
                if os.path.exists(camera_frames_dir):
                    # Count total frames directly in camera folder
                    frames = glob.glob(os.path.join(camera_frames_dir, "*.jpg"))
                    total_frames = len(frames)
                    print(f"Camera {camera_id}: {total_frames} total frames")
        
        # Show gesture distribution
        gesture_counts = self.gesture_labels['Gesture'].value_counts()
        print("\nGesture distribution:")
        for gesture, count in gesture_counts.items():
            print(f"  {gesture}: {count}")
        
        # Show training readiness info
        print(f"\nTraining configuration:")
        print(f"  Reading time cutoff: {self.reading_time_cutoff} seconds")
        print(f"  Minimum segment duration: {self.min_segment_duration} seconds")
        print(f"  Target FPS: {self.target_fps}")
        print(f"  Frame extraction FPS: {self.frame_extraction_fps}")
        print(f"  Output directory: {self.output_path}")
        print(f"  Frames directory: {self.frames_path}")

def main():
    parser = argparse.ArgumentParser(description="Post-process videos for model training")
    parser.add_argument("participant_id", help="Participant ID")
    parser.add_argument("--segment-duration", type=int, default=15, 
                       help="Duration of each segment in seconds (default: 15)")
    parser.add_argument("--base-path", default="dataset",
                       help="Base path for dataset (default: dataset)")
    parser.add_argument("--stats-only", action="store_true",
                       help="Only show statistics, don't process videos")
    parser.add_argument("--reading-cutoff", type=int, default=5,
                       help="Seconds to cut from start (reading time, default: 5)")
    parser.add_argument("--min-duration", type=int, default=3,
                       help="Minimum segment duration in seconds (default: 3)")
    parser.add_argument("--extract-frames", action="store_true",
                       help="Extract frames from segmented videos after processing")
    parser.add_argument("--frames-only", action="store_true",
                       help="Only extract frames from existing segmented videos")
    
    args = parser.parse_args()
    
    # Create processor
    processor = PostProcessor(args.participant_id, args.base_path)
    
    # Update parameters if provided
    processor.reading_time_cutoff = args.reading_cutoff
    processor.min_segment_duration = args.min_duration
    
    if args.stats_only:
        processor.get_processing_statistics()
    elif args.frames_only:
        # Only extract frames from existing segmented videos
        processor.extract_frames_from_segments()
        processor.get_processing_statistics()
    elif args.extract_frames:
        # Process videos and then extract frames
        processor.process_videos_and_frames(args.segment_duration)
        processor.get_processing_statistics()
    else:
        # Process videos only (original behavior)
        processor.process_videos(args.segment_duration)
        processor.get_processing_statistics()

if __name__ == "__main__":
    main()
