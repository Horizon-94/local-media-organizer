"""Existing current hybrid reader, gated ONLY by release publication truth."""
import sqlite3
from .search_jobs import SearchJobManager as CurrentSearchManager

class Build150SearchManager(CurrentSearchManager):
    def readiness(self):
        session=getattr(self,'session_store',None)
        if session is not None:
            with session.connect() as con:
                if getattr(session,'reuse_preparation',False) and session.prepared_readiness is not None:
                    return __import__('copy').deepcopy(session.prepared_readiness)
                report=self._publication_readiness(con,{'database_path':str(self.db_path),'database_error':'','database_open_mode':'readonly_session_snapshot'})
                if getattr(session,'reuse_preparation',False):session.prepared_readiness=__import__('copy').deepcopy(report)
                return report
        con,preflight=self._database_preflight()
        if con is None:return {'ready':False,'checks':{'database':False},'database_preflight':preflight}
        try:return self._publication_readiness(con,preflight)
        finally:con.close()

    def _publication_readiness(self,con,preflight):
        from .v2_release_search import enabled
        published=enabled(con) and bool(con.execute('''SELECT 1 FROM release_search_heads h
            JOIN release_registry r ON r.source_id=h.source_id AND r.revision=h.revision
            WHERE h.visible=1 AND r.present=1 AND r.committed=1 LIMIT 1''').fetchone())
        from .v2_semantic_search import bindings
        semantic=bool(bindings(con)) if published else False
        return {'ready':published,'checks':{'database':True,'search_publication':published},
            'database_preflight':preflight,'search_mode':'V2_CURRENT_HYBRID' if semantic else 'V2_CURRENT_TEXT',
            'semantic_vector_search_ready':semantic,'processing_engine':'BUILD150_ONLY'}

    def _active_scheduler_mode(self):
        # This compatibility value selects the existing *reader*, never a
        # processing driver. No scheduler_runs query or completion gate.
        return 'event_driven_v2'
