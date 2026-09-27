/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

package org.blender.blender;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import org.json.JSONObject;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;

/**
 * Minimal keep-alive for unattended Blender control/render work (UNATTENDED v1, Level 1).
 *
 * Why this exists: when BlenderActivity leaves the foreground, Android may
 * freeze/suspend the app process. The control bridge (TCP 127.0.0.1:17878,
 * socket thread -&gt; queue -&gt; bpy.app.timers -&gt; Blender main-thread bpy)
 * then stops executing in userspace while the TCP FD stays bound. A foreground
 * service gives the existing process foreground priority so the existing single
 * Blender runtime keeps ticking while the Activity is backgrounded.
 *
 * What this service does NOT do:
 * - no second Blender runtime, no second bpy interpreter;
 * - no duplicate RPC server, no Blender logic in Java;
 * - no networking beyond what the existing localhost bridge already does;
 * - no wake lock, no battery-exemption request (v1).
 *
 * "Foreground Service" here does NOT mean the Blender GUI stays foregrounded.
 * The Activity may disappear behind other apps; only this persistent
 * notification remains.
 */
public class BlenderControlService extends Service {

  private static final String TAG = "blender";

  /** Explicit stop action from the persistent notification. */
  public static final String ACTION_STOP =
      "org.blender.blender.BlenderControlService.ACTION_STOP";

  private static final String CHANNEL_ID = "blender_control";
  private static final int NOTIFICATION_ID = 17878;

  /** Same activation file as scripts/startup/bl_android_control.py. */
  private static final String CONTROL_CONFIG_PATH =
      "/storage/emulated/0/Download/blender-control.json";

  @Override
  public void onCreate() {
    super.onCreate();
    createChannel();
  }

  @Override
  public int onStartCommand(Intent intent, int flags, int startId) {
    if (intent != null && ACTION_STOP.equals(intent.getAction())) {
      Log.i(TAG, "[BlenderControlService] explicit stop requested");
      stopForeground(Service.STOP_FOREGROUND_REMOVE);
      stopSelf();
      return START_NOT_STICKY;
    }
    startKeepAlive();
    // If the system kills us under memory pressure, recreate so an active
    // job is not silently dropped. No new runtime is created on restart:
    // this service only holds process priority.
    return START_STICKY;
  }

  @Override
  public void onDestroy() {
    try {
      stopForeground(Service.STOP_FOREGROUND_REMOVE);
    } catch (Exception ignored) {
    }
    super.onDestroy();
  }

  @Override
  public IBinder onBind(Intent intent) {
    return null;
  }

  private void startKeepAlive() {
    Notification notification = buildNotification();
    try {
      if (Build.VERSION.SDK_INT >= 29) {
        startForeground(
            NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
      } else {
        startForeground(NOTIFICATION_ID, notification);
      }
      Log.i(TAG, "[BlenderControlService] foreground keep-alive active");
    } catch (Exception ex) {
      Log.w(TAG, "[BlenderControlService] startForeground failed", ex);
    }
  }

  private void createChannel() {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
      return;
    }
    NotificationManager manager =
        (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
    if (manager == null) {
      return;
    }
    NotificationChannel channel =
        new NotificationChannel(
            CHANNEL_ID,
            "Blender control",
            NotificationManager.IMPORTANCE_LOW);
    channel.setDescription(
        "Keeps an explicitly enabled Blender control/render session running while the app is in the background.");
    try {
      manager.createNotificationChannel(channel);
    } catch (Exception ex) {
      Log.w(TAG, "[BlenderControlService] cannot create notification channel", ex);
    }
  }

  private Notification buildNotification() {
    Intent openIntent =
        new Intent(this, BlenderActivity.class)
            .setAction(Intent.ACTION_MAIN)
            .addCategory(Intent.CATEGORY_LAUNCHER);
    int pendingFlags = PendingIntent.FLAG_UPDATE_CURRENT;
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
      pendingFlags |= PendingIntent.FLAG_IMMUTABLE;
    }
    PendingIntent open =
        PendingIntent.getActivity(this, 0, openIntent, pendingFlags);

    Intent stopIntent = new Intent(this, BlenderControlService.class).setAction(ACTION_STOP);
    PendingIntent stop =
        PendingIntent.getService(this, 1, stopIntent, pendingFlags);

    Notification.Builder builder;
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
      builder = new Notification.Builder(this, CHANNEL_ID);
    } else {
      builder = new Notification.Builder(this);
    }
    return builder
        .setContentTitle("Blender control active")
        .setContentText("Background scene/render work is enabled. Tap to return to Blender.")
        .setSmallIcon(android.R.drawable.stat_sys_upload_done)
        .setContentIntent(open)
        .setOngoing(true)
        .addAction(
            new Notification.Action.Builder(
                    android.R.drawable.ic_menu_close_clear_cancel, "Stop", stop)
                .build())
        .build();
  }

  /**
   * Start the keep-alive only when the operator explicitly enabled the
   * localhost control plane. Absent/disabled/malformed config -&gt; no service,
   * mirroring bl_android_control.py (never silently background-resident).
   */
  public static void startIfControlEnabled(Context context) {
    if (!isControlEnabled()) {
      Log.i(TAG, "[BlenderControlService] control not enabled; keep-alive not started");
      return;
    }
    try {
      Intent intent = new Intent(context, BlenderControlService.class);
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        context.startForegroundService(intent);
      } else {
        context.startService(intent);
      }
      Log.i(TAG, "[BlenderControlService] start requested (control enabled)");
    } catch (Exception ex) {
      Log.w(TAG, "[BlenderControlService] start failed", ex);
    }
  }

  /** Idempotent stop; safe to call when the service is not running. */
  public static void stop(Context context) {
    try {
      context.stopService(new Intent(context, BlenderControlService.class));
    } catch (Exception ex) {
      Log.w(TAG, "[BlenderControlService] stop failed", ex);
    }
  }

  private static boolean isControlEnabled() {
    try {
      File config = new File(CONTROL_CONFIG_PATH);
      if (!config.isFile() || !config.canRead()) {
        return false;
      }
      byte[] raw = Files.readAllBytes(config.toPath());
      if (raw.length == 0 || raw.length > 64 * 1024) {
        return false;
      }
      JSONObject data = new JSONObject(new String(raw, StandardCharsets.UTF_8));
      if (data.optBoolean("enabled", false) != true) {
        return false;
      }
      String token = data.optString("token", "");
      return token != null && token.length() >= 16;
    } catch (Exception ex) {
      return false;
    }
  }
}
