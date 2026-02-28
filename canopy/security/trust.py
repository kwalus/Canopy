"""
Trust management system for Canopy.

Implements EigenTrust-inspired reputation scoring and delete signal compliance tracking
to maintain network integrity and user privacy.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import logging
import json
import secrets
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum

from ..core.database import DatabaseManager

logger = logging.getLogger(__name__)


class TrustEvent(Enum):
    """Types of trust events that can affect reputation."""
    MESSAGE_DELIVERED = "message_delivered"
    DELETE_COMPLIED = "delete_complied"
    DELETE_VIOLATED = "delete_violated"
    KEY_SHARED = "key_shared"
    PEER_VERIFIED = "peer_verified"
    MALICIOUS_BEHAVIOR = "malicious_behavior"
    NETWORK_CONTRIBUTION = "network_contribution"


@dataclass
class DeleteSignal:
    """Represents a delete signal sent to a peer."""
    id: str
    target_peer_id: str
    data_type: str
    data_id: str
    reason: Optional[str]
    sent_at: datetime
    acknowledged_at: Optional[datetime] = None
    complied_at: Optional[datetime] = None
    status: str = "pending"  # pending, acknowledged, complied, violated
    
    def is_expired(self, timeout_hours: int = 24) -> bool:
        """Check if delete signal has expired without compliance."""
        if self.status in ['complied', 'violated']:
            return False
        
        expiry_time = self.sent_at + timedelta(hours=timeout_hours)
        return datetime.now(timezone.utc) > expiry_time
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'id': self.id,
            'target_peer_id': self.target_peer_id,
            'data_type': self.data_type,
            'data_id': self.data_id,
            'reason': self.reason,
            'sent_at': self.sent_at.isoformat(),
            'acknowledged_at': self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            'complied_at': self.complied_at.isoformat() if self.complied_at else None,
            'status': self.status
        }


class TrustManager:
    """Manages trust scores and delete signal compliance."""
    
    def __init__(self, db_manager: DatabaseManager):
        """Initialize trust manager with database connection."""
        self.db = db_manager
        self.default_trust_score = 100
        self.min_trust_score = 0
        self.max_trust_score = 100
        self.delete_timeout_hours = 24
    
    def get_trust_score(self, peer_id: str) -> int:
        """Get current trust score for a peer."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT score FROM trust_scores WHERE peer_id = ?
                """, (peer_id,))
                
                row = cursor.fetchone()
                return row['score'] if row else self.default_trust_score
                
        except Exception as e:
            logger.error(f"Failed to get trust score for {peer_id}: {e}")
            return self.default_trust_score
    
    def update_trust_score(self, peer_id: str, event: TrustEvent, 
                          score_delta: Optional[int] = None, 
                          reason: Optional[str] = None) -> int:
        """Update trust score based on an event."""
        # Default score deltas for different events
        default_deltas = {
            TrustEvent.MESSAGE_DELIVERED: 1,
            TrustEvent.DELETE_COMPLIED: 5,
            TrustEvent.DELETE_VIOLATED: -20,
            TrustEvent.KEY_SHARED: 2,
            TrustEvent.PEER_VERIFIED: 3,
            TrustEvent.MALICIOUS_BEHAVIOR: -30,
            TrustEvent.NETWORK_CONTRIBUTION: 3
        }
        
        if score_delta is None:
            score_delta = default_deltas.get(event, 0)
        
        try:
            with self.db.get_connection() as conn:
                # Get current score or create new entry
                cursor = conn.execute("""
                    SELECT score FROM trust_scores WHERE peer_id = ?
                """, (peer_id,))
                
                row = cursor.fetchone()
                current_score = row['score'] if row else self.default_trust_score
                
                # Calculate new score within bounds
                new_score = max(
                    self.min_trust_score,
                    min(self.max_trust_score, current_score + score_delta)
                )
                
                # Update or insert trust score
                if row:
                    conn.execute("""
                        UPDATE trust_scores 
                        SET score = ?, last_interaction = CURRENT_TIMESTAMP,
                            compliance_events = compliance_events + ?,
                            violation_events = violation_events + ?,
                            notes = ?
                        WHERE peer_id = ?
                    """, (
                        new_score,
                        1 if event == TrustEvent.DELETE_COMPLIED else 0,
                        1 if event == TrustEvent.DELETE_VIOLATED else 0,
                        f"{event.value}: {reason}" if reason else event.value,
                        peer_id
                    ))
                else:
                    conn.execute("""
                        INSERT INTO trust_scores 
                        (peer_id, score, compliance_events, violation_events, notes)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        peer_id, new_score,
                        1 if event == TrustEvent.DELETE_COMPLIED else 0,
                        1 if event == TrustEvent.DELETE_VIOLATED else 0,
                        f"{event.value}: {reason}" if reason else event.value
                    ))
                
                conn.commit()
                
                logger.info(f"Updated trust score for {peer_id}: {current_score} -> {new_score} ({event.value})")
                return new_score
                
        except Exception as e:
            logger.error(f"Failed to update trust score for {peer_id}: {e}")
            return self.get_trust_score(peer_id)

    def set_trust_score(self, peer_id: str, score: int, reason: Optional[str] = None) -> int:
        """Set trust score directly (e.g., manual adjustment)."""
        try:
            clamped_score = max(self.min_trust_score, min(self.max_trust_score, int(score)))
        except (TypeError, ValueError):
            logger.error(f"Invalid trust score provided for {peer_id}: {score}")
            return self.get_trust_score(peer_id)

        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT score FROM trust_scores WHERE peer_id = ?",
                    (peer_id,)
                )
                row = cursor.fetchone()

                note = f"manual: {reason}" if reason else "manual"
                if row:
                    conn.execute("""
                        UPDATE trust_scores
                        SET score = ?, last_interaction = CURRENT_TIMESTAMP, notes = ?
                        WHERE peer_id = ?
                    """, (clamped_score, note, peer_id))
                else:
                    conn.execute("""
                        INSERT INTO trust_scores
                        (peer_id, score, compliance_events, violation_events, notes)
                        VALUES (?, ?, 0, 0, ?)
                    """, (peer_id, clamped_score, note))

                conn.commit()
                logger.info(f"Set trust score for {peer_id}: {clamped_score} ({note})")
                return clamped_score
        except Exception as e:
            logger.error(f"Failed to set trust score for {peer_id}: {e}")
            return self.get_trust_score(peer_id)
    
    def get_all_trust_scores(self) -> Dict[str, Dict[str, Any]]:
        """Get all trust scores with metadata."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT peer_id, score, last_interaction, compliance_events, 
                           violation_events, notes
                    FROM trust_scores
                    ORDER BY score DESC
                """)
                
                scores = {}
                for row in cursor.fetchall():
                    scores[row['peer_id']] = {
                        'score': row['score'],
                        'last_interaction': row['last_interaction'],
                        'compliance_events': row['compliance_events'],
                        'violation_events': row['violation_events'],
                        'notes': row['notes'],
                        'is_trusted': row['score'] >= 50  # Trust threshold
                    }
                
                return scores
                
        except Exception as e:
            logger.error(f"Failed to get all trust scores: {e}")
            return {}
    
    def create_delete_signal(self, target_peer_id: str, data_type: str, 
                           data_id: str, reason: Optional[str] = None) -> Optional[DeleteSignal]:
        """Create a delete signal for a peer."""
        try:
            signal_id = secrets.token_hex(16)
            signal = DeleteSignal(
                id=signal_id,
                target_peer_id=target_peer_id,
                data_type=data_type,
                data_id=data_id,
                reason=reason,
                sent_at=datetime.now(timezone.utc)
            )
            
            # Store in database
            success = self.db.create_delete_signal(
                signal_id, target_peer_id, data_type, data_id, reason
            )
            
            if success:
                logger.info(f"Created delete signal {signal_id} for peer {target_peer_id}")
                return signal
            else:
                return None
                
        except Exception as e:
            logger.error(f"Failed to create delete signal: {e}")
            return None
    
    def acknowledge_delete_signal(self, signal_id: str) -> bool:
        """Acknowledge receipt of a delete signal."""
        try:
            success = self.db.update_delete_signal_status(signal_id, "acknowledged")
            if success:
                logger.info(f"Acknowledged delete signal: {signal_id}")
            return success
        except Exception as e:
            logger.error(f"Failed to acknowledge delete signal: {e}")
            return False
    
    def comply_with_delete_signal(self, signal_id: str, peer_id: str) -> bool:
        """Mark a delete signal as complied with and update trust score."""
        try:
            success = self.db.update_delete_signal_status(signal_id, "complied")
            if success:
                # Reward compliance with positive trust score
                self.update_trust_score(
                    peer_id, 
                    TrustEvent.DELETE_COMPLIED, 
                    reason=f"Complied with delete signal {signal_id}"
                )
                logger.info(f"Marked delete signal {signal_id} as complied")
            return success
        except Exception as e:
            logger.error(f"Failed to mark delete signal as complied: {e}")
            return False
    
    def violate_delete_signal(self, signal_id: str, peer_id: str) -> bool:
        """Mark a delete signal as violated and penalize trust score."""
        try:
            success = self.db.update_delete_signal_status(signal_id, "violated")
            if success:
                # Penalize violation with negative trust score
                self.update_trust_score(
                    peer_id, 
                    TrustEvent.DELETE_VIOLATED, 
                    reason=f"Violated delete signal {signal_id}"
                )
                logger.warning(f"Marked delete signal {signal_id} as violated by {peer_id}")
            return success
        except Exception as e:
            logger.error(f"Failed to mark delete signal as violated: {e}")
            return False
    
    def get_pending_delete_signals(self, target_peer_id: Optional[str] = None) -> List[DeleteSignal]:
        """Get pending delete signals, optionally filtered by target peer."""
        try:
            with self.db.get_connection() as conn:
                if target_peer_id:
                    cursor = conn.execute("""
                        SELECT * FROM delete_signals 
                        WHERE target_peer_id = ? AND status = 'pending'
                        ORDER BY sent_at DESC
                    """, (target_peer_id,))
                else:
                    cursor = conn.execute("""
                        SELECT * FROM delete_signals 
                        WHERE status = 'pending'
                        ORDER BY sent_at DESC
                    """)
                
                signals = []
                for row in cursor.fetchall():
                    signal = DeleteSignal(
                        id=row['id'],
                        target_peer_id=row['target_peer_id'],
                        data_type=row['data_type'],
                        data_id=row['data_id'],
                        reason=row['reason'],
                        sent_at=datetime.fromisoformat(row['sent_at']),
                        acknowledged_at=datetime.fromisoformat(row['acknowledged_at']) if row['acknowledged_at'] else None,
                        complied_at=datetime.fromisoformat(row['complied_at']) if row['complied_at'] else None,
                        status=row['status']
                    )
                    signals.append(signal)
                
                return signals
                
        except Exception as e:
            logger.error(f"Failed to get pending delete signals: {e}")
            return []
    
    def check_expired_delete_signals(self) -> List[Tuple[str, str]]:
        """Check for expired delete signals and mark as violations."""
        expired_signals = []
        
        try:
            pending_signals = self.get_pending_delete_signals()
            
            for signal in pending_signals:
                if signal.is_expired(self.delete_timeout_hours):
                    # Mark as violated
                    self.violate_delete_signal(signal.id, signal.target_peer_id)
                    expired_signals.append((signal.id, signal.target_peer_id))
            
            if expired_signals:
                logger.warning(f"Found {len(expired_signals)} expired delete signals")
            
        except Exception as e:
            logger.error(f"Failed to check expired delete signals: {e}")
        
        return expired_signals
    
    def is_peer_trusted(self, peer_id: str, threshold: int = 50) -> bool:
        """Check if a peer is trusted based on their trust score."""
        return self.get_trust_score(peer_id) >= threshold
    
    def get_trusted_peers(self, threshold: int = 50) -> List[str]:
        """Get list of trusted peer IDs."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT peer_id FROM trust_scores WHERE score >= ?
                    ORDER BY score DESC
                """, (threshold,))
                
                return [row['peer_id'] for row in cursor.fetchall()]
                
        except Exception as e:
            logger.error(f"Failed to get trusted peers: {e}")
            return []
    
    def get_trust_statistics(self) -> Dict[str, Any]:
        """Get overall trust network statistics."""
        try:
            with self.db.get_connection() as conn:
                # Basic statistics
                cursor = conn.execute("""
                    SELECT 
                        COALESCE(COUNT(*), 0) as total_peers,
                        COALESCE(AVG(score), 0.0) as average_score,
                        COALESCE(MIN(score), 0) as min_score,
                        COALESCE(MAX(score), 0) as max_score,
                        COALESCE(SUM(CASE WHEN score >= 50 THEN 1 ELSE 0 END), 0) as trusted_peers,
                        COALESCE(SUM(compliance_events), 0) as total_compliance,
                        COALESCE(SUM(violation_events), 0) as total_violations
                    FROM trust_scores
                """)
                
                stats = dict(cursor.fetchone())
                
                # Delete signal statistics
                cursor = conn.execute("""
                    SELECT 
                        COALESCE(COUNT(*), 0) as total_signals,
                        COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) as pending_signals,
                        COALESCE(SUM(CASE WHEN status = 'complied' THEN 1 ELSE 0 END), 0) as complied_signals,
                        COALESCE(SUM(CASE WHEN status = 'violated' THEN 1 ELSE 0 END), 0) as violated_signals
                    FROM delete_signals
                """)
                
                signal_stats = dict(cursor.fetchone())
                stats.update(signal_stats)
                
                # Calculate compliance rate  
                total_resolved = (signal_stats.get('complied_signals', 0) + 
                                signal_stats.get('violated_signals', 0))
                stats['compliance_rate'] = (
                    signal_stats.get('complied_signals', 0) / total_resolved * 100
                    if total_resolved > 0 else 100.0
                )
                
                return stats
                
        except Exception as e:
            logger.error(f"Failed to get trust statistics: {e}")
            return {}
